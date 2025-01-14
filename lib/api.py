import json
import sys
import time
from typing import List, Tuple, Optional
from datetime import datetime, timedelta

import requests
from requests.structures import CaseInsensitiveDict
from PIL import Image
from aiohttp import ClientSession

from .kool import Fore, _print as print
from .errors import APIException, AxisOutOfRange, APIOffline
from .cli import arguments as args


class Pixel(list):
    """
    Describes a pixel

    Attributes:
        x: int - the X (horizontal) co-ordinate
        y: int - the Y (vertical) co-ordinate
        rgb: str - the hexadecimal value
        hex: str - the hexadecimal value
    """

    def __init__(self, x: int, y: int, rgb: str):
        self.x = x
        self.y = y
        self.rgb = rgb
        self.hex = rgb
        super().__init__((x, y, rgb))


class Api:
    """
    OOP API Container
    """

    def __init__(self, base: str = "https://pixels.pythondiscord.com", *, auth: str, session: ClientSession = None):
        self.session = requests.session()
        self.base = base
        self.auth = auth
        # self.session = session

        self.max_width, self.max_height = self.get_size()
        # sync all ratelimits
        self.sync_ratelimit("get_pixel")
        self.sync_ratelimit("get_pixels")
        self.sync_ratelimit("set_pixel")

    def __del__(self):
        self.session.close()

    def _adjust_height(self, x, y):
        if self.max_width >= x >= 0 or self.max_height >= y >= 0:  # canvas size has probably changed.
            # self.max_width, self.max_height = self.get_size()
            pass
            # While writing this, I realised if the canvas re-expands there's no way to detect this without
            # sending yet another request.

    def _request(self, uri: str, method: str = "GET", **kwargs):
        method = method.upper()
        kwargs.setdefault("headers", {})
        kwargs["headers"].setdefault(
            "Authorization",
            "Bearer " + self.auth
        )
        return_content = kwargs.pop("return_content", "json")
        if args.verbose:
            print(f"{Fore.RED}[DEBUG] {Fore.LIGHTBLACK_EX}{method}-ing {uri}...")
        response = self.session.request(method, self.base+uri, **kwargs)
        if args.verbose:
            print(f"{Fore.RED}[DEBUG] {Fore.LIGHTBLACK_EX}sent {method} to {uri}.")

        # Error handling
        if response.status_code == 422:
            raise AxisOutOfRange(response.status_code, response.json()["detail"], message="Malformed request.")
        if response.status_code in range(500, 600):  # server error:
            raise APIOffline(response.status_code, f"Pixels server appears to be down.")

        # Lets not 429
        self.wait_out_ratelimit(response.headers)
        # NOTE: This PAUSES the ENTIRE program for ONE endpoint's cooldown.
        # A better alternative would be checking against datetimes in some container bucket thing.
        # For now, it's not so much of an issue.
        # However, in terms of protection scripts, this can make or break the program's success.

        if response.status_code != 200:
            if args.verbose:
                print(f"{Fore.RED}[DEBUG] {Fore.LIGHTBLACK_EX}non-200 {response.status_code} on {uri}. retrying.")
            return self._request(uri, method, **kwargs)

        # We need a special case for HEAD requests with no body
        if method == "HEAD":
            return response.status_code, {}
        if return_content is None:
            return response.status_code
        attr = getattr(response, return_content)
        if callable(attr):
            data = attr()
        else:
            data = attr
        return response.status_code, data

    def get_size(self) -> Tuple[int, int]:
        """
        Fetches the size of the canvas.

        :return: width, height
        """
        # self.sync_ratelimit("get_size")
        status, data = self._request("/get_size")
        return data["width"], data["height"]

    def get_pixel(self, x: int, y: int) -> Pixel:
        """
        Fetches a pixel from the remote canvas.

        :param x: The X (horizontal) co-ordinate of the target pixel
        :param y: X but Y
        :return: Pixel - The Found pixel.
        :raises: ValueError - the co-ordinates were out of range
        """
        self.sync_ratelimit("get_pixel")
        status, data = self._request("/get_pixel", "GET", params={"x": x, "y": y})
        return Pixel(*data.values())

    def blind_set_pixel(self, x: int, y: int, colour: str) -> bool:
        """
        Sets a pixel on the canvas.
        Unlike set_pixel, this will set it without checking first.

        :param x: The X (horizontal) co-ordinate of the target pixel
        :param y: You can figure this one out
        :param colour: The hexadecimal colour
        :return:
        """
        self.sync_ratelimit("set_pixel")
        status, data = self._request(
            "/set_pixel",
            "POST",
            json={"x": x, "y": y, "rgb": colour}
        )
        if status != 200:
            raise APIException(status, "Unknown error.", message=json.dumps(data, indent=2))
        return True

    def set_pixel(self, x: int, y: int, colour: str) -> Optional[bool]:
        """
        Sets a pixel on the canvas.

        :param x: Guess
        :param y: Guess more
        :param colour: #hex000
        :return:
        """
        pixel = self.get_pixel(x, y)
        if pixel.hex == colour:
            return
        return self.blind_set_pixel(x, y, colour)

    def get_pixels(self, resize_to: Tuple[int, int] = None) -> Image:
        """
        Downloads the entire canvas

        :param resize_to: The width, height pair to resize to. If not provided, will not resize.
        :return: PIL.Image
        """
        self.sync_ratelimit("get_pixels")
        status, image_data = self._request(
            "/get_pixels",
            return_content="content"
        )
        size = self.get_size()

        image = Image.frombytes(
            "RGB",
            size,
            image_data
        )
        if resize_to:
            image.resize(resize_to, Image.NEAREST)
        return image

    @staticmethod
    def wait_out_ratelimit(headers: CaseInsensitiveDict):
        remaining = int(headers.get("requests-remaining", 0))
        soft_cooldown = headers.get("requests-reset", ...)
        hard_cooldown = headers.get("cooldown-reset", ...)
        if hard_cooldown is ...:
            if soft_cooldown is ...:
                return  # no cooldown
            soft_cooldown = float(soft_cooldown)
            if remaining == 0:  # Soft cooldown
                expire = datetime.now() + timedelta(seconds=soft_cooldown)
                print(
                    f"{Fore.CYAN}[RATELIMITER] {Fore.LIGHTYELLOW_EX}On {Fore.LIGHTGREEN_EX}soft cooldown"
                    f"{Fore.LIGHTYELLOW_EX} for {Fore.LIGHTCYAN_EX}{soft_cooldown} seconds{Fore.LIGHTYELLOW_EX} "
                    f"(until {Fore.LIGHTCYAN_EX}{expire.strftime('%X')}{Fore.LIGHTYELLOW_EX})."
                )
                time.sleep(soft_cooldown)
        else:
            hard_cooldown = float(hard_cooldown)
            expire = datetime.now() + timedelta(seconds=hard_cooldown)
            print(
                f"{Fore.CYAN}[RATELIMITER] {Fore.LIGHTYELLOW_EX}On {Fore.RED}hard cooldown"
                f"{Fore.LIGHTYELLOW_EX} for {Fore.LIGHTCYAN_EX}{hard_cooldown} seconds{Fore.LIGHTYELLOW_EX} "
                f"(until {Fore.LIGHTCYAN_EX}{expire.strftime('%X')}{Fore.LIGHTYELLOW_EX})."
            )
            time.sleep(hard_cooldown)

    def sync_ratelimit(self, endpoint: str = "set_pixel"):
        """
        Simply sends a HEAD request and waits for the timeout.

        :return:
        """
        print(Fore.RED+"[DEBUG]"+Fore.LIGHTBLACK_EX+" Syncing ratelimit for", endpoint, verbose=True)
        self._request("/"+endpoint.lower(), "HEAD")
        print(Fore.RED + "[DEBUG]" + Fore.LIGHTBLACK_EX + " Synced ratelimit for", endpoint, verbose=True)


def get_pixels(img) -> List[Tuple[int, int, Tuple[int, int, int]]]:
    """
    Fetches an array of [x, y, (r, g, b)] in the image, with x, y being the x,y co-ords and rgb being the rgb values.

    :param img: the PIL.Image
    :return: List[Tuple[int, int, Tuple[int, int, int]]]
    """
    pixels = []
    for y in range(img.height):
        for x in range(img.width):
            pixels.append((x, y, img.getpixel((x, y))))
    return pixels


def set_pixel(*at: int, colour: str, token: str, base: str = "https://pixels.pythondiscord.com"):
    """
    Handles all the fuss setting pixels in places.

    :param at: A pair of x, y co-ords to set the pixel at
    :param colour: The hex value to set
    :param token: Your API token
    :param base: The base API URL. You should change this in main.py if needs be, not here.
    :return: None
    """
    if "dev" in sys.argv:
        print(f"{Fore.RED}[DEBUG] {Fore.LIGHTBLACK_EX}Args for setting pixel: at={at} colour={colour} token={{no}}")
    try:
        preflight_response = requests.get(
            base + "/get_pixel", params={"x": at[0], "y": at[1]}, headers={"Authorization": "Bearer " + token}
        )
        handle_sane_ratelimit(preflight_response)
        if preflight_response.json()["rgb"] == colour:
            print(f"{Fore.CYAN}[API] {at} was already set. ignoring.")
            return 300
        response = requests.post(
            base + "/set_pixel",
            json={"x": at[0], "y": at[1], "rgb": colour},
            headers={"Authorization": "Bearer " + token},
        )
    except (requests.HTTPError, requests.HTTPError, requests.RequestException):
        print(f"{Fore.YELLOW}[WARNING] {Fore.WHITE}Exception while setting a pixel. Retrying in 5 seconds.")
        time.sleep(5)
        return set_pixel(*at, colour=colour, token=token)
    handle_sane_ratelimit(response)
    if response.status_code == 429:
        # try again
        print(f"{Fore.CYAN}[API] {Fore.LIGHTRED_EX}set_pixel call previously failed due to ratelimit. Retrying {at}.")
        return set_pixel(*at, colour=colour, token=token)
    if response.status_code != 200:
        if response.headers.get("content-type", "null") == "application/json":
            print(
                f"{Fore.RED}[ERROR] {Fore.LIGHTWHITE_EX}Non-200 pixel set code. "
                f"Data:\n{json.dumps(response.json(), indent=2)}"
            )
            return -1
        else:
            print(f"{Fore.RED}[ERROR] {Fore.LIGHTWHITE_EX}Non-200 pixel set code. Data:\n{response.text}")
            return -1
    return 200


def handle_sane_ratelimit(res):
    """
    Handles ratelimits in a way that prevents getting 429s, but also handles actual 429s.

    Soft cooldown is when the function detects that the next request would be ratelimited
    Hard cooldown is when the response status was 429, indicating a ratelimit.

    :param res: The response object
    :return: Nothing
    """
    remaining = int(res.headers.get("requests-remaining", 0))
    if res.status_code == 429:
        reset = float(res.headers["cooldown-reset"])
        print(
            f"{Fore.CYAN}[RATELIMITER] {Fore.LIGHTRED_EX}On hard cooldown for",
            reset,
            "seconds.\nThis only really tends to happen if you're running multiple instances.\n"
            "If you're unsure why you've got a 429, check:\n"
            "1. You haven't restarted the program while it was on a cooldown (it'll reset the handler)\n"
            "2. You aren't running the program elsewhere\n"
            "3. Your token hasn't been leaked. If you believe it has, reset it ASAP.",
            file=sys.stderr,
        )
        time.sleep(reset)
    else:
        if remaining == 0:
            try:
                reset = float(res.headers["requests-reset"])
            except KeyError:
                headers_joined = "\n".join(f"{k}: {v}" for k, v in res.headers.items())
                print(
                    f"{Fore.RED}[DEBUG][RATELIMITER] {Fore.LIGHTBLACK_EX} A lack of ratelimit headers were sent:\n"
                    + headers_joined
                    + "\nGoing to ignore ratelimit handling for this request, and pray we haven't"
                    " stumbled upon a hard limit."
                )
                return
            print(
                f"{Fore.CYAN}[RATELIMITER] {Fore.LIGHTYELLOW_EX}On soft cooldown for",
                reset,
                "seconds.",
            )
            time.sleep(reset)
