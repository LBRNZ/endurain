"""OneLapFit utility functions for API interactions and token management."""

import hashlib
from datetime import datetime, timedelta, timezone
import httpx
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

import core.cryptography as core_cryptography
import core.logger as core_logger
import core.config as core_config

import users.users_integrations.crud as user_integrations_crud
import users.users_integrations.models as user_integrations_models

import users.users.crud as users_crud

from core.database import SessionLocal

# OneLapFit API base URL
ONELAPFIT_API_BASE = "https://rfs-fitness.rfsvr.com/api/v1/app"


def hash_password(password: str) -> str:
    """Hash password using MD5 (as required by OneLapFit API)."""
    return hashlib.md5(password.encode()).hexdigest().lower()


async def login_onelapfit(email: str, password: str) -> str:
    """
    Login to OneLapFit and retrieve access token.

    Args:
        email: User email address
        password: User password (will be MD5 hashed)

    Returns:
        Access token string

    Raises:
        HTTPException: If login fails
    """
    try:
        core_logger.print_to_log(
            f"OneLapFit login: Starting login process for email {email}"
        )
        hashed_password = hash_password(password)
        core_logger.print_to_log(
            f"OneLapFit login: Password hashed successfully"
        )
        
        async with httpx.AsyncClient() as client:
            core_logger.print_to_log(
                f"OneLapFit login: Creating HTTP request to {ONELAPFIT_API_BASE}/login"
            )
            response = await client.post(
                f"{ONELAPFIT_API_BASE}/login",
                json={
                    "account": email,
                    "password": hashed_password,
                },
                timeout=30.0,
            )
            
            core_logger.print_to_log(
                f"OneLapFit login: Received response with status {response.status_code}"
            )
            
            if response.status_code != 200:
                core_logger.print_to_log(
                    f"OneLapFit login failed with status {response.status_code}: {response.text}",
                    "error",
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="OneLapFit login failed. Check your credentials.",
                )
            
            core_logger.print_to_log(
                f"OneLapFit login: Parsing JSON response"
            )
            data = response.json()
            core_logger.print_to_log(
                f"OneLapFit login: Response data code: {data.get('code')}"
            )
            
            if data.get("code") != 200:
                core_logger.print_to_log(
                    f"OneLapFit API returned error: {data.get('error')}",
                    "error",
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"OneLapFit login failed: {data.get('error')}",
                )
            
            core_logger.print_to_log(
                f"OneLapFit login: Extracting token from response"
            )
            token = data.get("data", {}).get("token")
            if not token:
                core_logger.print_to_log(
                    f"OneLapFit login: No token in response data",
                    "error",
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No token returned from OneLapFit",
                )
            
            core_logger.print_to_log(
                f"OneLapFit login: Token extracted successfully, length: {len(token)}"
            )
            return token
    except HTTPException:
        raise
    except httpx.RequestError as err:
        core_logger.print_to_log(
            f"OneLapFit API request error: {err}",
            "error",
            exc=err,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to connect to OneLapFit service",
        ) from err


def fetch_user_integrations_and_validate_token(
    user_id: int, db: Session
) -> user_integrations_models.UsersIntegrations | None:
    """
    Fetch user integrations and validate OneLapFit token exists.

    Args:
        user_id: User ID
        db: Database session

    Returns:
        User integrations object or None if token is invalid/missing

    Raises:
        HTTPException: If user integrations not found
    """
    user_integrations = user_integrations_crud.get_user_integrations_by_user_id(
        user_id, db
    )

    if user_integrations is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User information not found",
        )

    if user_integrations.onelapfit_token is None:
        return None

    return user_integrations


async def fetch_onelapfit_activities(
    token: str,
    start_time: int,
    end_time: int,
    page: int = 0,
) -> dict:
    """
    Fetch riding list from OneLapFit API.

    Args:
        token: OneLapFit access token
        start_time: Unix timestamp for start date
        end_time: Unix timestamp for end date
        page: Page number for pagination (0-indexed)

    Returns:
        Response data from OneLapFit API

    Raises:
        HTTPException: If API call fails
    """
    try:
        headers = {
            "Authorization": token,
        }
        
        core_logger.print_to_log(
            f"OneLapFit activities: Fetching activities from {start_time} to {end_time}"
        )
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{ONELAPFIT_API_BASE}/record/riding/list",
                params={
                    "start_time": start_time,
                    "end_time": end_time,
                    "p": page,
                    "source": "all",
                    "data_type": "all",
                },
                headers=headers,
                timeout=30.0,
            )
            
            core_logger.print_to_log(
                f"OneLapFit activities: Received HTTP {response.status_code}"
            )
            
            if response.status_code != 200:
                core_logger.print_to_log(
                    f"OneLapFit activities fetch failed with status {response.status_code}: {response.text}",
                    "error",
                )
                raise HTTPException(
                    status_code=status.HTTP_424_FAILED_DEPENDENCY,
                    detail="Unable to fetch OneLapFit activities",
                )
            
            data = response.json()
            core_logger.print_to_log(
                f"OneLapFit activities: Response code: {data.get('code')}, has data: {bool(data.get('data'))}"
            )
            
            if data.get("code") != 200:
                core_logger.print_to_log(
                    f"OneLapFit API returned error: {data.get('error')}",
                    "error",
                )
                raise HTTPException(
                    status_code=status.HTTP_424_FAILED_DEPENDENCY,
                    detail=f"OneLapFit API error: {data.get('error')}",
                )
            
            result = data.get("data", {})
            core_logger.print_to_log(
                f"OneLapFit activities: Returning data with keys: {list(result.keys()) if result else 'empty'}"
            )
            return result
    except httpx.RequestError as err:
        core_logger.print_to_log(
            f"OneLapFit activities fetch request error: {err}",
            "error",
            exc=err,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to connect to OneLapFit service",
        ) from err


async def fetch_onelapfit_overview(
    token: str,
    start_time: int = 0,
    end_time: int = None,
) -> dict:
    """
    Fetch riding overview from OneLapFit API.

    Args:
        token: OneLapFit access token
        start_time: Unix timestamp for start date (default: 0)
        end_time: Unix timestamp for end date (default: current time)

    Returns:
        Response data from OneLapFit API
    """
    if end_time is None:
        end_time = int(datetime.now(timezone.utc).timestamp())
    
    try:
        headers = {
            "Authorization": token,
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{ONELAPFIT_API_BASE}/record/riding/overview",
                params={
                    "start_time": start_time,
                    "end_time": end_time,
                    "source": "table",
                    "data_type": "all",
                },
                headers=headers,
                timeout=30.0,
            )
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_424_FAILED_DEPENDENCY,
                    detail="Unable to fetch OneLapFit overview",
                )
            
            data = response.json()
            if data.get("code") != 200:
                raise HTTPException(
                    status_code=status.HTTP_424_FAILED_DEPENDENCY,
                    detail=f"OneLapFit API error: {data.get('error')}",
                )
            
            return data.get("data", {})
    except httpx.RequestError as err:
        core_logger.print_to_log(
            f"OneLapFit overview fetch request error: {err}",
            "error",
            exc=err,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to connect to OneLapFit service",
        ) from err


async def download_fit_file(url: str) -> bytes:
    """
    Download FIT file from OneLapFit URL.

    Args:
        url: Full URL to FIT file

    Returns:
        File content as bytes

    Raises:
        HTTPException: If download fails
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=60.0)
            
            if response.status_code != 200:
                core_logger.print_to_log(
                    f"FIT file download failed with status {response.status_code}",
                    "error",
                )
                raise HTTPException(
                    status_code=status.HTTP_424_FAILED_DEPENDENCY,
                    detail="Unable to download FIT file",
                )
            
            return response.content
    except httpx.RequestError as err:
        core_logger.print_to_log(
            f"FIT file download request error: {err}",
            "error",
            exc=err,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to download FIT file",
        ) from err
