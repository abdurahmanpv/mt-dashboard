"""
sharepoint_upload.py
====================
Uploads the dashboard Excel to SharePoint using a service account
(standard M365 username + password — no Azure AD app registration needed).

Environment variables (set as GitHub Secrets):
  SP_SITE_URL     Full URL of the SharePoint site
                  e.g. https://waycominc.sharepoint.com/sites/DataTeam
  SP_FOLDER_PATH  Server-relative path to the target folder
                  e.g. /sites/DataTeam/Shared Documents/Mileage Tracker/CEO Dashboard/files
  SP_USERNAME     Service account email  e.g. automation@waycominc.com
  SP_PASSWORD     Service account password

How to find SP_FOLDER_PATH:
  Open the SharePoint folder in a browser → note the URL after /sites/DataTeam/
  The server-relative path is  /sites/DataTeam/<library>/<subfolder(s)>
  Common library names: "Shared Documents" or "Documents"
"""

import os
import logging

log = logging.getLogger(__name__)


def upload_to_sharepoint(local_path: str, filename: str = None) -> str:
    """
    Upload a file to the SharePoint folder defined by SP_FOLDER_PATH.

    Replaces an existing file with the same name.

    Parameters
    ----------
    local_path : path to the local file to upload
    filename   : filename to use in SharePoint (defaults to basename of local_path)

    Returns
    -------
    str : server-relative URL of the uploaded file
    """
    try:
        from office365.runtime.auth.user_credential import UserCredential
        from office365.sharepoint.client_context import ClientContext
    except ImportError:
        raise ImportError(
            "office365-rest-python-client not installed. "
            "Run: pip install Office365-REST-Python-Client"
        )

    if filename is None:
        filename = os.path.basename(local_path)

    site_url    = os.environ["SP_SITE_URL"]
    folder_path = os.environ["SP_FOLDER_PATH"]
    username    = os.environ["SP_USERNAME"]
    password    = os.environ["SP_PASSWORD"]

    log.info(f"  Connecting to SharePoint as {username} ...")
    ctx = ClientContext(site_url).with_credentials(
        UserCredential(username, password)
    )

    file_size = os.path.getsize(local_path)
    log.info(f"  Uploading {filename} ({file_size / 1024:.1f} KB) to {folder_path} ...")

    with open(local_path, "rb") as f:
        file_content = f.read()

    target_folder = ctx.web.get_folder_by_server_relative_url(folder_path)
    uploaded = target_folder.upload_file(filename, file_content).execute_query()

    result_url = uploaded.serverRelativeUrl
    log.info(f"  SharePoint upload complete → {result_url}")
    return result_url
