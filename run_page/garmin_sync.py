#!/usr/bin/env python3
"""
Python 3 API wrapper for Garmin Connect to get your statistics.
Uses python-garminconnect with auto-refresh and modern DI token support.
"""

import argparse
import asyncio
import base64
import datetime as dt
import json
import logging
import os

os.environ["GARTH_TELEMETRY_ENABLED"] = "false"
import sys
import time
import traceback
import zipfile
from io import BytesIO

import aiofiles
import httpx
from config import FOLDER_DICT, JSON_FILE, SQL_FILE
from garmin_device_adaptor import process_garmin_data
from lxml import etree

try:
    from garminconnect.client import Client as GarminClient
    from garminconnect import Garmin as GC
except ImportError:
    GarminClient = None
    GC = None

try:
    import garth
except ImportError:
    garth = None

from utils import make_activities_file

logger = logging.getLogger(__name__)

TIME_OUT = httpx.Timeout(240.0, connect=360.0)
GARMIN_COM_URL_DICT = {
    "SSO_URL_ORIGIN": "https://sso.garmin.com",
    "SSO_URL": "https://sso.garmin.com/sso",
    "MODERN_URL": "https://connectapi.garmin.com",
    "SIGNIN_URL": "https://sso.garmin.com/sso/signin",
    "UPLOAD_URL": "https://connectapi.garmin.com/upload-service/upload/",
    "ACTIVITY_URL": "https://connectapi.garmin.com/activity-service/activity/{activity_id}",
}

GARMIN_CN_URL_DICT = {
    "SSO_URL_ORIGIN": "https://sso.garmin.com",
    "SSO_URL": "https://sso.garmin.cn/sso",
    "MODERN_URL": "https://connectapi.garmin.cn",
    "SIGNIN_URL": "https://sso.garmin.cn/sso/signin",
    "UPLOAD_URL": "https://connectapi.garmin.cn/upload-service/upload/",
    "ACTIVITY_URL": "https://connectapi.garmin.cn/activity-service/activity/{activity_id}",
}


class Garmin:
    def __init__(
        self,
        secret_string=None,
        auth_domain="COM",
        is_only_running=False,
        email=None,
        password=None,
    ):
        """
        Init module
        """
        self.req = httpx.AsyncClient(timeout=TIME_OUT)
        self.auth_domain = str(auth_domain).upper() if auth_domain else "COM"
        self.URL_DICT = (
            GARMIN_CN_URL_DICT
            if self.auth_domain == "CN"
            else GARMIN_COM_URL_DICT
        )
        self.modern_url = self.URL_DICT.get("MODERN_URL")
        self.is_only_running = is_only_running
        self.upload_url = self.URL_DICT.get("UPLOAD_URL")
        self.activity_url = self.URL_DICT.get("ACTIVITY_URL")
        self.email = email
        self.password = password

        domain = "garmin.cn" if self.auth_domain == "CN" else "garmin.com"
        self.gc_client = GarminClient(domain=domain) if GarminClient else None

        authenticated = False

        # 1. Try loading from secret_string
        if secret_string and self.gc_client:
            tokenstore = self._parse_tokenstore(secret_string)
            if tokenstore:
                try:
                    self.gc_client.loads(tokenstore)
                    if self.gc_client.is_authenticated:
                        if self.gc_client._token_expires_soon():
                            logger.info("Garmin token expires soon, refreshing via DI refresh token...")
                            try:
                                self.gc_client._refresh_di_token()
                            except Exception as e:
                                logger.warning(f"Proactive DI refresh failed: {e}")
                        authenticated = True
                except Exception as e:
                    logger.warning(f"Could not load tokenstore into GarminClient: {e}")

        # 2. If not authenticated, try credentials login
        if not authenticated and self.email and self.password and self.gc_client:
            logger.info("Attempting Garmin login with email and password...")
            try:
                self.gc_client.login(self.email, self.password)
                authenticated = True
                logger.info("Garmin login succeeded!")
            except Exception as e:
                logger.error(f"Garmin login failed: {e}")

        # 3. Garth fallback (e.g. for legacy CN or legacy tokens)
        auth_header = ""
        if self.gc_client and self.gc_client.di_token:
            auth_header = f"Bearer {self.gc_client.di_token}"
        elif secret_string and garth:
            try:
                if self.auth_domain == "CN":
                    garth.configure(domain="garmin.cn", ssl_verify=False)
                garth.client.loads(secret_string)
                if garth.client.oauth2_token.expired:
                    garth.client.refresh_oauth2()
                auth_header = str(garth.client.oauth2_token)
            except Exception as e:
                logger.warning(f"Garth fallback failed: {e}")

        if not auth_header:
            raise GarminConnectAuthenticationError(
                "Authentication failed: No valid token or login credentials provided"
            )

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/79.0.3945.88 Safari/537.36",
            "origin": self.URL_DICT.get("SSO_URL_ORIGIN"),
            "nk": "NT",
            "Authorization": auth_header,
        }

    @staticmethod
    def _parse_tokenstore(secret_string):
        if not secret_string:
            return None
        # Try direct JSON with di_token
        try:
            data = json.loads(secret_string)
            if isinstance(data, dict) and "di_token" in data:
                return secret_string
        except Exception:
            pass

        # Try Base64-encoded JSON
        try:
            decoded = base64.b64decode(secret_string.strip()).decode("utf-8")
            data = json.loads(decoded)
            if isinstance(data, dict) and "di_token" in data:
                return decoded
            if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], dict):
                oauth2 = data[1]
                access_token = oauth2.get("access_token")
                refresh_token = oauth2.get("refresh_token")
                if access_token or refresh_token:
                    store = {
                        "di_token": access_token,
                        "di_refresh_token": refresh_token,
                        "di_client_id": "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
                    }
                    return json.dumps(store)
        except Exception:
            pass
        return None

    def _refresh_auth(self):
        refreshed = False
        if self.gc_client and self.gc_client.di_refresh_token:
            try:
                logger.info("Refreshing Garmin DI token...")
                self.gc_client._refresh_di_token()
                refreshed = True
                logger.info("Successfully refreshed Garmin DI token.")
            except Exception as e:
                logger.warning(f"Failed to refresh DI token: {e}")

        if not refreshed and self.email and self.password and self.gc_client:
            try:
                logger.info("Attempting full login with credentials...")
                self.gc_client.login(self.email, self.password)
                refreshed = True
                logger.info("Full relogin succeeded.")
            except Exception as e:
                logger.error(f"Full relogin failed: {e}")

        if refreshed and self.gc_client and self.gc_client.di_token:
            self.headers["Authorization"] = f"Bearer {self.gc_client.di_token}"
            return True

        raise GarminConnectAuthenticationError("Could not refresh token or re-login")

    async def fetch_data(self, url, retrying=False):
        """
        Fetch and return data
        """
        try:
            response = await self.req.get(url, headers=self.headers)
            if response.status_code == 401 and not retrying:
                logger.info("fetch_data got 401 Unauthorized, refreshing auth...")
                self._refresh_auth()
                return await self.fetch_data(url, retrying=True)
            if response.status_code == 429:
                raise GarminConnectTooManyRequestsError("Too many requests")
            logger.debug(f"fetch_data got response code {response.status_code}")
            response.raise_for_status()
            return response.json()
        except Exception as err:
            if not retrying:
                logger.debug("Exception during fetch_data, attempting token refresh: %s", err)
                try:
                    self._refresh_auth()
                    return await self.fetch_data(url, retrying=True)
                except Exception:
                    pass
            logger.error("Exception occurred during data retrieval: %s", err)
            raise GarminConnectConnectionError("Error connecting") from err

    async def get_activities(self, start, limit):
        """
        Fetch available activities
        """
        url = f"{self.modern_url}/activitylist-service/activities/search/activities?start={start}&limit={limit}"
        if self.is_only_running:
            url = url + "&activityType=running"
        return await self.fetch_data(url)

    async def get_activity_summary(self, activity_id):
        """
        Fetch activity summary
        """
        url = f"{self.modern_url}/activity-service/activity/{activity_id}"
        return await self.fetch_data(url)

    async def download_activity(self, activity_id, file_type="gpx", retrying=False):
        url = f"{self.modern_url}/download-service/export/{file_type}/activity/{activity_id}"
        if file_type == "fit":
            url = f"{self.modern_url}/download-service/files/activity/{activity_id}"
        logger.info(f"Download activity from {url}")
        try:
            response = await self.req.get(url, headers=self.headers)
            if response.status_code == 401 and not retrying:
                logger.info("download_activity got 401 Unauthorized, refreshing auth...")
                self._refresh_auth()
                return await self.download_activity(
                    activity_id, file_type=file_type, retrying=True
                )
            response.raise_for_status()
            return response.read()
        except Exception as err:
            if not retrying:
                try:
                    self._refresh_auth()
                    return await self.download_activity(
                        activity_id, file_type=file_type, retrying=True
                    )
                except Exception:
                    pass
            raise

    async def upload_activities_original_from_strava(
        self, datas, use_fake_garmin_device=False
    ):
        print(
            "start upload activities to garmin!, use_fake_garmin_device:",
            use_fake_garmin_device,
        )
        for data in datas:
            with open(data.filename, "wb") as f:  # noqa: ASYNC230
                f.writelines(data.content)
            with open(data.filename, "rb") as f:  # noqa: ASYNC230
                file_body = process_garmin_data(f, use_fake_garmin_device)
            files = {"file": (data.filename, file_body)}

            try:
                res = await self.req.post(
                    self.upload_url, files=files, headers=self.headers
                )
                os.remove(data.filename)
            except Exception as e:  # noqa: BLE001
                print(str(e))
                # just pass for now
                continue
            try:
                resp = res.json()["detailedImportResult"]
                print("garmin upload success: ", resp)
            except Exception as e:  # noqa: BLE001
                print("garmin upload failed: ", e)
        await self.req.aclose()

    async def upload_activity_from_file(self, file):
        print("Uploading " + str(file))
        with open(file, "rb") as f:  # noqa: ASYNC230
            file_body = BytesIO(f.read())
        files = {"file": (file, file_body)}

        try:
            res = await self.req.post(
                self.upload_url, files=files, headers=self.headers
            )
        except Exception as e:  # noqa: BLE001
            print(str(e))
            # just pass for now
            return
        try:
            resp = res.json()["detailedImportResult"]
            print("garmin upload success: ", resp)
        except Exception as e:  # noqa: BLE001
            print("garmin upload failed: ", e)

    async def upload_activities_files(self, files):
        print("start upload activities to garmin!")

        await gather_with_concurrency(
            10,
            [self.upload_activity_from_file(file=f) for f in files],
        )

        await self.req.aclose()


class GarminConnectHttpError(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


class GarminConnectConnectionError(Exception):
    """Raised when communication ended in error."""

    def __init__(self, status):
        """Initialize."""
        super().__init__(status)
        self.status = status


class GarminConnectTooManyRequestsError(Exception):
    """Raised when rate limit is exceeded."""

    def __init__(self, status):
        """Initialize."""
        super().__init__(status)
        self.status = status


class GarminConnectAuthenticationError(Exception):
    """Raised when login returns wrong result."""

    def __init__(self, status):
        """Initialize."""
        super().__init__(status)
        self.status = status


def get_info_text_value(summary_infos, key_name):
    if summary_infos.get(key_name) is None:
        return ""
    return str(summary_infos.get(key_name))


def create_element(parent, tag, text):
    elem = etree.SubElement(parent, tag)
    elem.text = text
    elem.tail = "\n"
    return elem


def add_summary_info(file_data, summary_infos, fields=None):
    if summary_infos is None:
        return file_data
    try:
        root = etree.fromstring(file_data)
        extensions_node = etree.Element("extensions")
        extensions_node.text = "\n"
        extensions_node.tail = "\n"
        if fields is None:
            fields = [
                "distance",
                "average_hr",
                "average_speed",
                "start_time",
                "end_time",
                "moving_time",
                "elapsed_time",
            ]
        for field in fields:
            create_element(
                extensions_node, field, get_info_text_value(summary_infos, field)
            )
        root.insert(0, extensions_node)
        return etree.tostring(root, encoding="utf-8", pretty_print=True)
    except etree.XMLSyntaxError as e:
        print(f"Failed to parse file data: {e!s}")
    except Exception as e:  # noqa: BLE001
        print(f"Failed to append summary info to file data: {e!s}")
    return file_data


async def download_garmin_data(
    client, activity_id, file_type="gpx", summary_infos=None
):
    folder = FOLDER_DICT.get(file_type, "gpx")
    try:
        file_data = await client.download_activity(activity_id, file_type=file_type)
        if summary_infos is not None and file_type == "gpx":
            file_data = add_summary_info(file_data, summary_infos.get(activity_id))
        file_path = os.path.join(folder, f"{activity_id}.{file_type}")
        need_unzip = False
        if file_type == "fit":
            file_path = os.path.join(folder, f"{activity_id}.zip")
            need_unzip = True
        async with aiofiles.open(file_path, "wb") as fb:
            await fb.write(file_data)
        if need_unzip:
            zip_file = zipfile.ZipFile(file_path, "r")
            for file_info in zip_file.infolist():
                zip_file.extract(file_info, folder)
                if file_info.filename.endswith(".fit"):
                    os.rename(
                        os.path.join(folder, f"{activity_id}_ACTIVITY.fit"),
                        os.path.join(folder, f"{activity_id}.fit"),
                    )
                elif file_info.filename.endswith(".gpx"):
                    os.rename(
                        os.path.join(folder, f"{activity_id}_ACTIVITY.gpx"),
                        os.path.join(FOLDER_DICT["gpx"], f"{activity_id}.gpx"),
                    )
                else:
                    os.remove(os.path.join(folder, file_info.filename))
            os.remove(file_path)
    except Exception as e:  # noqa: BLE001
        print(f"Failed to download activity {activity_id}: {e!s}")
        traceback.print_exc()


async def get_activity_id_list(client, start=0):
    activities = await client.get_activities(start, 100)
    if len(activities) > 0:
        ids = [str(a.get("activityId", "")) for a in activities]
        print("Syncing Activity IDs")
        return ids + await get_activity_id_list(client, start + 100)
    else:
        return []


async def gather_with_concurrency(n, tasks):
    semaphore = asyncio.Semaphore(n)

    async def sem_task(task):
        async with semaphore:
            return await task

    return await asyncio.gather(*(sem_task(task) for task in tasks))


def get_downloaded_ids(folder):
    return [i.split(".")[0] for i in os.listdir(folder) if not i.startswith(".")]


def get_garmin_summary_infos(activity_summary, activity_id):
    garmin_summary_infos = {}
    try:
        summary_dto = activity_summary.get("summaryDTO")
        garmin_summary_infos["distance"] = summary_dto.get("distance")
        garmin_summary_infos["average_hr"] = summary_dto.get("averageHR")
        garmin_summary_infos["average_speed"] = summary_dto.get("averageSpeed")
        start_time = dt.datetime.fromisoformat(
            summary_dto.get("startTimeGMT")[:-1] + "+00:00"  # noqa: FURB162
        )
        duration_second = summary_dto.get("duration")
        end_time = start_time + dt.timedelta(seconds=duration_second)
        garmin_summary_infos["start_time"] = start_time.isoformat()
        garmin_summary_infos["end_time"] = end_time.isoformat()
        garmin_summary_infos["moving_time"] = summary_dto.get("movingDuration")
        garmin_summary_infos["elapsed_time"] = summary_dto.get("elapsedDuration")
    except Exception as e:  # noqa: BLE001
        print(f"Failed to get activity summary {activity_id}: {e!s}")
    return garmin_summary_infos


async def download_new_activities(
    secret_string,
    auth_domain,
    downloaded_ids,
    is_only_running,
    folder,
    file_type,
    email=None,
    password=None,
):
    client = Garmin(
        secret_string=secret_string,
        auth_domain=auth_domain,
        is_only_running=is_only_running,
        email=email,
        password=password,
    )
    # because I don't find a para for after time, so I use garmin-id as filename
    # to find new run to generate
    activity_ids = await get_activity_id_list(client)
    to_generate_garmin_ids = list(set(activity_ids) - set(downloaded_ids))
    print(f"{len(to_generate_garmin_ids)} new activities to be downloaded")

    to_generate_garmin_id2title = {}
    garmin_summary_infos_dict = {}
    for id in to_generate_garmin_ids:
        try:
            activity_summary = await client.get_activity_summary(id)
            activity_title = activity_summary.get("activityName", "")
            to_generate_garmin_id2title[id] = activity_title
            garmin_summary_infos_dict[id] = get_garmin_summary_infos(
                activity_summary, id
            )
        except Exception as e:  # noqa: BLE001
            print(f"Failed to get activity summary {id}: {e!s}")
            continue

    start_time = time.time()
    await gather_with_concurrency(
        10,
        [
            download_garmin_data(
                client, id, file_type=file_type, summary_infos=garmin_summary_infos_dict
            )
            for id in to_generate_garmin_ids
        ],
    )
    print(f"Download finished. Elapsed {time.time()-start_time} seconds")

    await client.req.aclose()
    return to_generate_garmin_ids, to_generate_garmin_id2title


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "secret_string",
        nargs="?",
        default=os.environ.get("GARMIN_SECRET_STRING"),
        help="secret_string from get_garmin_secret.py or GARMIN_SECRET_STRING env",
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("GARMIN_EMAIL"),
        help="Garmin account email",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("GARMIN_PASSWORD"),
        help="Garmin account password",
    )
    parser.add_argument(
        "--is-cn",
        dest="is_cn",
        action="store_true",
        help="if garmin account is cn",
    )
    parser.add_argument(
        "--only-run",
        dest="only_run",
        action="store_true",
        help="if is only for running",
    )
    parser.add_argument(
        "--tcx",
        dest="download_file_type",
        action="store_const",
        const="tcx",
        default="gpx",
        help="to download personal documents or ebook",
    )
    parser.add_argument(
        "--fit",
        dest="download_file_type",
        action="store_const",
        const="fit",
        default="gpx",
        help="to download personal documents or ebook",
    )
    options = parser.parse_args()
    secret_string = options.secret_string
    email = options.email
    password = options.password
    auth_domain = "CN" if options.is_cn else "COM"  # Default to COM if not specified
    file_type = options.download_file_type
    is_only_running = options.only_run

    if secret_string is None and not (email and password):
        print("Missing secret_string or credentials (GARMIN_EMAIL, GARMIN_PASSWORD)")
        sys.exit(1)

    folder = FOLDER_DICT.get(file_type, "gpx")
    # make gpx or tcx dir
    if not os.path.exists(folder):
        os.mkdir(folder)
    downloaded_ids = get_downloaded_ids(folder)

    if file_type == "fit":
        gpx_folder = FOLDER_DICT["gpx"]
        if not os.path.exists(gpx_folder):
            os.mkdir(gpx_folder)
        downloaded_gpx_ids = get_downloaded_ids(gpx_folder)
        # merge downloaded_ids:list
        downloaded_ids = list(set(downloaded_ids + downloaded_gpx_ids))

    loop = asyncio.get_event_loop()
    future = asyncio.ensure_future(
        download_new_activities(
            secret_string,
            auth_domain,
            downloaded_ids,
            is_only_running,
            folder,
            file_type,
            email=email,
            password=password,
        )
    )
    loop.run_until_complete(future)
    new_ids, id2title = future.result()
    # fit may contain gpx(maybe upload by user)
    if file_type == "fit":
        make_activities_file(
            SQL_FILE,
            FOLDER_DICT["gpx"],
            JSON_FILE,
            file_suffix="gpx",
            activity_title_dict=id2title,
        )
    make_activities_file(
        SQL_FILE, folder, JSON_FILE, file_suffix=file_type, activity_title_dict=id2title
    )
