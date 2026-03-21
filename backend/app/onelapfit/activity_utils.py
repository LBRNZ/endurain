"""OneLapFit activity processing utilities."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from io import BytesIO
import logging

from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from timezonefinder import TimezoneFinder

import core.logger as core_logger
import core.config as core_config

import activities.activity.schema as activities_schema
import activities.activity.crud as activities_crud
import activities.activity.utils as activities_utils

import users.users_integrations.models as user_integrations_models
import users.users.crud as users_crud
import users.users_default_gear.utils as user_default_gear_utils

import users.users_privacy_settings.crud as users_privacy_settings_crud
import users.users_privacy_settings.models as users_privacy_settings_models
import users.users_privacy_settings.utils as users_privacy_settings_utils

import gears.gear.crud as gears_crud

import onelapfit.utils as onelapfit_utils

import websocket.manager as websocket_manager

from core.database import SessionLocal


async def fetch_and_process_activities(
    token: str,
    start_date: datetime,
    end_date: datetime,
    user_id: int,
    user_integrations: user_integrations_models.UsersIntegrations,
    ws_manager: websocket_manager.WebSocketManager,
    db: Session,
    is_startup: bool = False,
) -> int:
    """
    Fetch and process OneLapFit activities within date range.

    Args:
        token: OneLapFit access token
        start_date: Start datetime for activity search
        end_date: End datetime for activity search
        user_id: User ID
        user_integrations: User integrations record
        ws_manager: WebSocket manager for notifications
        db: Database session
        is_startup: Whether this is a startup sync

    Returns:
        Count of processed activities
    """
    onelapfit_activities = None
    
    # Convert dates to Unix timestamps
    start_time = int(start_date.timestamp())
    end_time = int(end_date.timestamp())
    
    try:
        # Fetch activities from OneLapFit
        core_logger.print_to_log(
            f"User {user_id}: Fetching OneLapFit activities for range {start_date} to {end_date}"
        )
        activities_data = await onelapfit_utils.fetch_onelapfit_activities(
            token=token,
            start_time=start_time,
            end_time=end_time,
            page=0,
        )
        
        core_logger.print_to_log(
            f"User {user_id}: Received activities data: {activities_data}"
        )
        onelapfit_activities = activities_data
    except Exception as err:
        core_logger.print_to_log(
            f"User {user_id}: Error fetching OneLapFit activities: {str(err)}",
            "error",
            exc=err,
        )
        if not is_startup:
            raise HTTPException(
                status_code=status.HTTP_424_FAILED_DEPENDENCY,
                detail="Unable to fetch OneLapFit activities",
            ) from err
        return 0
    
    if not onelapfit_activities or not onelapfit_activities.get("days"):
        core_logger.print_to_log(
            f"User {user_id}: No new OneLapFit activities found (activities_data: {onelapfit_activities})"
        )
        return 0
    
    user = users_crud.get_user_by_id(user_id, db)
    if user is None:
        if not is_startup:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        return 0
    
    user_privacy_settings = (
        users_privacy_settings_crud.get_user_privacy_settings_by_user_id(user.id, db)
    )
    
    processed_activities = []
    
    # Process each day's activities
    for day_timestamp, day_data in onelapfit_activities.get("days", {}).items():
        for activity_info in day_data.get("info", []):
            try:
                processed_activity = await process_activity(
                    activity_info=activity_info,
                    user_id=user_id,
                    user_privacy_settings=user_privacy_settings,
                    token=token,
                    user_integrations=user_integrations,
                    ws_manager=ws_manager,
                    db=db,
                )
                if processed_activity:
                    processed_activities.append(processed_activity)
            except Exception as err:
                core_logger.print_to_log(
                    f"User {user_id}: Error processing OneLapFit activity: {str(err)}",
                    "error",
                    exc=err,
                )
                continue
    
    return len(processed_activities) if processed_activities else 0


async def process_activity(
    activity_info: dict,
    user_id: int,
    user_privacy_settings: users_privacy_settings_models.UsersPrivacySettings,
    token: str,
    user_integrations: user_integrations_models.UsersIntegrations,
    ws_manager: websocket_manager.WebSocketManager,
    db: Session,
) -> Optional[activities_schema.Activity]:
    """
    Process a single OneLapFit activity and create/update database record.

    Args:
        activity_info: Activity data from OneLapFit API
        user_id: User ID
        user_privacy_settings: User privacy settings
        token: OneLapFit token
        user_integrations: User integrations record
        ws_manager: WebSocket manager
        db: Database session

    Returns:
        Created/updated Activity object or None if skipped
    """
    try:
        # Parse activity data
        activity_dict = parse_activity(
            activity_info=activity_info,
            user_id=user_id,
            user_privacy_settings=user_privacy_settings,
            db=db,
        )
        
        if activity_dict is None:
            return None
        
        # Create or update activity in database
        activity = activities_crud.create_activity(
            activity=activities_schema.ActivityCreate(**activity_dict),
            user_id=user_id,
            db=db,
        )
        
        # Download and store FIT file
        try:
            fit_file_url = activity_info.get("fileAddr")
            if fit_file_url:
                fit_content = await onelapfit_utils.download_fit_file(fit_file_url)
                # Store FIT file would go here (depends on your file storage system)
                core_logger.print_to_log(
                    f"User {user_id}: Downloaded FIT file for activity {activity.id}"
                )
        except Exception as err:
            core_logger.print_to_log(
                f"User {user_id}: Failed to download FIT file: {str(err)}",
                "warning",
            )
        
        # Notify via WebSocket
        try:
            await ws_manager.broadcast_user(
                user_id=user_id,
                message={
                    "type": "activity_imported",
                    "activity_id": activity.id,
                    "name": activity.name,
                },
            )
        except Exception as err:
            core_logger.print_to_log(
                f"User {user_id}: Failed to send WebSocket notification: {str(err)}",
                "warning",
            )
        
        return activity
    except Exception as err:
        core_logger.print_to_log(
            f"User {user_id}: Error in process_activity: {str(err)}",
            "error",
            exc=err,
        )
        return None


def parse_activity(
    activity_info: dict,
    user_id: int,
    user_privacy_settings: users_privacy_settings_models.UsersPrivacySettings,
    db: Session,
) -> Optional[dict]:
    """
    Parse OneLapFit activity data into Activity model format.

    Args:
        activity_info: Raw activity info from OneLapFit API
        user_id: User ID
        user_privacy_settings: User privacy settings
        db: Database session

    Returns:
        Dict with activity data ready for database insertion or None if invalid
    """
    try:
        # Extract basic activity info
        start_time = activity_info.get("start_time", 0)
        elapsed_time = activity_info.get("total_time", 0)  # in seconds
        moving_time = activity_info.get("time", 0)  # in seconds
        distance = activity_info.get("distance", 0)  # in meters
        elevation_gain = activity_info.get("elevation", 0)
        
        # Create datetime from Unix timestamp
        activity_date = datetime.fromtimestamp(start_time, tz=timezone.utc)
        
        # Find timezone
        tf = TimezoneFinder()
        tz_str = core_config.TZ
        
        # Determine activity type
        activity_type = "Ride"  # OneLapFit is currently for cycling
        
        # Calculate speed if available
        if moving_time > 0 and distance > 0:
            avg_speed = (distance / 1000) / (moving_time / 3600)  # km/h
        else:
            avg_speed = 0
        
        # Calculate power metrics if available
        avg_watts = activity_info.get("AP", 0)
        normalized_power = activity_info.get("NP", 0)
        ftp = activity_info.get("FTP", 0)
        tss = activity_info.get("TSS", 0)
        
        # Prepare activity dictionary
        activity_dict = {
            "name": activity_info.get("name", f"{activity_type} - {activity_date.strftime('%Y-%m-%d')}"),
            "type": activity_type,
            "start_date": activity_date,
            "elapsed_time": elapsed_time,
            "moving_time": moving_time,
            "distance": distance / 1000,  # Convert to km
            "elevation_gain": elevation_gain,
            "elevation_loss": 0,  # Not provided by OneLapFit
            "max_elevation": None,
            "min_elevation": None,
            "avg_speed": avg_speed,
            "max_speed": 0,
            "avg_heartrate": 0,
            "max_heartrate": 0,
            "avg_watts": avg_watts,
            "normalized_power": normalized_power,
            "ftp": ftp,
            "tss": tss,
            "calories": activity_info.get("cal", 0),
            "kudos_count": 0,
            "comment_count": 0,
            "athlete_count": None,
            "visibility": users_privacy_settings_utils.get_activity_visibility(
                user_privacy_settings
            ),
            "timezone": tz_str,
            "onelapfit_id": activity_info.get("fileKey", "").replace(".fit", ""),
        }
        
        # Check if activity already exists
        existing = activities_crud.get_activity_by_onelapfit_id_from_user_id(
            activity_dict["onelapfit_id"], user_id, db
        )
        if existing:
            core_logger.print_to_log(
                f"User {user_id}: Activity {activity_dict['onelapfit_id']} already exists"
            )
            return None
        
        return activity_dict
    except Exception as err:
        core_logger.print_to_log(
            f"User {user_id}: Error parsing OneLapFit activity: {str(err)}",
            "error",
            exc=err,
        )
        return None
