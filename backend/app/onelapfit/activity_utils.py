"""OneLapFit activity processing utilities."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from io import BytesIO
import logging
import tempfile
import os

from sqlalchemy.orm import Session
from fastapi import HTTPException, status
from timezonefinder import TimezoneFinder

import core.logger as core_logger
import core.config as core_config

import activities.activity.schema as activities_schema
import activities.activity.crud as activities_crud
import activities.activity.utils as activities_utils
import activities.activity_streams.schema as activity_streams_schema
import activities.activity_streams.crud as activity_streams_crud

import users.users_integrations.models as user_integrations_models
import users.users.crud as users_crud
import users.users_default_gear.utils as user_default_gear_utils

import users.users_privacy_settings.crud as users_privacy_settings_crud
import users.users_privacy_settings.models as users_privacy_settings_models
import users.users_privacy_settings.utils as users_privacy_settings_utils

import gears.gear.crud as gears_crud

import fit.utils as fit_utils

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
        activity_to_store = parse_activity(
            activity_info=activity_info,
            user_id=user_id,
            user_privacy_settings=user_privacy_settings,
            db=db,
        )
        
        if activity_to_store is None:
            return None
        
        # Create activity in database
        activity = await activities_crud.create_activity(
            activity=activity_to_store,
            websocket_manager=ws_manager,
            db=db,
        )
        
        # Download and parse FIT file
        try:
            fit_file_url = activity_info.get("fileAddr")
            if fit_file_url and activity.id:
                fit_content = await onelapfit_utils.download_fit_file(fit_file_url)
                
                # Save FIT content to a temporary file for parsing
                with tempfile.NamedTemporaryFile(
                    suffix=".fit", delete=False, mode="wb"
                ) as temp_fit_file:
                    temp_fit_file.write(fit_content)
                    temp_fit_path = temp_fit_file.name
                
                try:
                    # Parse FIT file to extract activity streams and metadata
                    parsed_fit_data = fit_utils.parse_fit_file(
                        temp_fit_path, db, activity_info.get("name", "Ride")
                    )
                    
                    core_logger.print_to_log(
                        f"User {user_id}: Parsed FIT file for activity {activity.id}"
                    )
                    
                    # Extract and store activity streams
                    activity_streams = activities_utils.parse_activity_streams_from_file(
                        parsed_fit_data, activity.id
                    )
                    
                    if activity_streams:
                        activity_streams_crud.create_activity_streams(
                            activity_streams, db
                        )
                        core_logger.print_to_log(
                            f"User {user_id}: Stored {len(activity_streams)} activity streams for activity {activity.id}"
                        )
                finally:
                    # Clean up temporary FIT file
                    try:
                        os.unlink(temp_fit_path)
                    except Exception as err:
                        core_logger.print_to_log(
                            f"User {user_id}: Failed to delete temporary FIT file: {str(err)}",
                            "warning",
                        )
        except Exception as err:
            core_logger.print_to_log(
                f"User {user_id}: Failed to download/parse FIT file: {str(err)}",
                "warning",
            )
        
        # Notify via WebSocket
        try:
            await ws_manager.send_message(
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
) -> Optional[activities_schema.Activity]:
    """
    Parse OneLapFit activity data into Activity schema format.

    Args:
        activity_info: Raw activity info from OneLapFit API
        user_id: User ID
        user_privacy_settings: User privacy settings
        db: Database session

    Returns:
        Activity schema object ready for database insertion or None if invalid
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
        end_date = datetime.fromtimestamp(start_time + elapsed_time, tz=timezone.utc)
        
        # Find timezone
        tz_str = core_config.TZ
        
        # Determine activity type (1 = Ride for cycling)
        activity_type = 1  # OneLapFit is currently for cycling
        
        # Calculate speed if available (in m/s for average_speed field)
        if moving_time > 0 and distance > 0:
            avg_speed = distance / moving_time  # m/s
        else:
            avg_speed = 0.0
        
        # Get power metrics if available
        avg_watts = activity_info.get("AP", 0) or None
        normalized_power = activity_info.get("NP", 0) or None
        tss = activity_info.get("TSS", 0) or None
        calories = activity_info.get("cal", 0) or None
        
        # Check if activity already exists before creating
        onelapfit_id = activity_info.get("fileKey", "").replace(".fit", "")
        existing = activities_crud.get_activity_by_onelapfit_id_from_user_id(
            onelapfit_id, user_id, db
        )
        if existing:
            core_logger.print_to_log(
                f"User {user_id}: Activity {onelapfit_id} already exists"
            )
            return None
        
        # Create Activity schema object
        activity_to_store = activities_schema.Activity(
            user_id=user_id,
            name=activity_info.get("name", f"Ride - {activity_date.strftime('%Y-%m-%d')}"),
            distance=int(distance),  # in meters
            activity_type=activity_type,
            start_time=activity_date.strftime("%Y-%m-%dT%H:%M:%S"),
            end_time=end_date.strftime("%Y-%m-%dT%H:%M:%S"),
            timezone=tz_str,
            total_elapsed_time=float(elapsed_time),  # in seconds
            total_timer_time=float(moving_time),  # in seconds
            elevation_gain=int(elevation_gain) if elevation_gain else None,
            elevation_loss=None,  # Not provided by OneLapFit
            average_speed=avg_speed if avg_speed > 0 else None,
            max_speed=None,  # Not provided by OneLapFit
            average_power=int(avg_watts) if avg_watts else None,
            max_power=None,  # Not provided by OneLapFit
            normalized_power=int(normalized_power) if normalized_power else None,
            average_hr=None,  # Not provided by OneLapFit
            max_hr=None,  # Not provided by OneLapFit
            average_cad=None,  # Not provided by OneLapFit
            max_cad=None,  # Not provided by OneLapFit
            calories=int(calories) if calories else None,
            visibility=users_privacy_settings_utils.visibility_to_int(
                user_privacy_settings.default_activity_visibility
            ),
            onelapfit_id=onelapfit_id,
            hide_start_time=user_privacy_settings.hide_activity_start_time or False,
            hide_location=user_privacy_settings.hide_activity_location or False,
            hide_map=user_privacy_settings.hide_activity_map or False,
            hide_hr=user_privacy_settings.hide_activity_hr or False,
            hide_power=user_privacy_settings.hide_activity_power or False,
            hide_cadence=user_privacy_settings.hide_activity_cadence or False,
            hide_elevation=user_privacy_settings.hide_activity_elevation or False,
            hide_speed=user_privacy_settings.hide_activity_speed or False,
            hide_pace=user_privacy_settings.hide_activity_pace or False,
            hide_laps=user_privacy_settings.hide_activity_laps or False,
            hide_workout_sets_steps=user_privacy_settings.hide_activity_workout_sets_steps or False,
            hide_gear=user_privacy_settings.hide_activity_gear or False,
        )
        
        return activity_to_store
    except Exception as err:
        core_logger.print_to_log(
            f"User {user_id}: Error parsing OneLapFit activity: {str(err)}",
            "error",
            exc=err,
        )
        return None
