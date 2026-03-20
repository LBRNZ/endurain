# OneLapFit Integration Guide

## Overview

The OneLapFit integration enables users to link their OneLapFit accounts and automatically import their cycling activities into Endurain. This document provides a comprehensive guide to the integration architecture, API endpoints, and implementation details.

## Architecture

The OneLapFit integration follows the same modular architecture as other integrations (Strava, Garmin Connect) in Endurain:

```
┌─────────────────────────────────────────────────────────────┐
│                     Frontend (Vue.js)                       │
│              - onelapfitService.js                          │
│              - UI Components                                │
│              - i18n translations                            │
└────────────────────┬────────────────────────────────────────┘
                     │ HTTP/REST API
┌────────────────────▼────────────────────────────────────────┐
│               Backend (FastAPI)                             │
│        ┌─────────────────────────────────────────┐          │
│        │  /api/v1/onelapfit Routes               │          │
│        │  - PUT /link                            │          │
│        │  - GET /activities                      │          │
│        │  - DELETE /unlink                       │          │
│        └─────────────────────────────────────────┘          │
│        ┌─────────────────────────────────────────┐          │
│        │  Core Modules                           │          │
│        │  - onelapfit/router.py                  │          │
│        │  - onelapfit/utils.py                   │          │
│        │  - onelapfit/activity_utils.py          │          │
│        │  - onelapfit/schema.py                  │          │
│        └─────────────────────────────────────────┘          │
│        ┌─────────────────────────────────────────┐          │
│        │  Database Models                        │          │
│        │  - users_integrations.onelapfit_token   │          │
│        └─────────────────────────────────────────┘          │
└────────────────────┬────────────────────────────────────────┘
                     │ HTTPS API Calls
┌────────────────────▼────────────────────────────────────────┐
│              OneLapFit API Service                          │
│        https://rfs-fitness.rfsvr.com/api/v1/app            │
└─────────────────────────────────────────────────────────────┘
```

## Backend Implementation

### 1. Module Structure

**Location:** `backend/app/onelapfit/`

#### Files:
- **__init__.py** - Module initializer
- **schema.py** - Pydantic request/response models
- **utils.py** - Core OneLapFit API interaction functions
- **activity_utils.py** - Activity processing and parsing
- **router.py** - FastAPI route definitions

### 2. Authentication Flow

OneLapFit uses email/password-based authentication (not OAuth):

```
User Credentials
     │
     ▼
[PUT /onelapfit/link] ──► POST login API
     │                        │
     │◄─ MD5 hash password ◄──┘
     │
     ▼
Receive access token
     │
     ▼
Encrypt with Fernet
     │
     ▼
Store in DB (users_integrations.onelapfit_token)
     │
     ▼
Return success response
```

### 3. API Endpoints

#### Link OneLapFit Account
```
PUT /api/v1/onelapfit/link
Content-Type: application/json
Authorization: Bearer <access_token>

Request Body:
{
  "email": "user@example.com",
  "password": "user_password"
}

Response:
{
  "detail": "OneLapFit linked successfully for user <user_id>"
}
```

#### Fetch Activities
```
GET /api/v1/onelapfit/activities?start_date=2024-01-01T00:00:00&end_date=2024-12-31T23:59:59
Authorization: Bearer <access_token>

Response:
{
  "status": "Background task started",
  "message": "Fetching OneLapFit activities in background"
}

Background Task:
- Fetches all activities within date range
- Downloads FIT files
- Parses activity data
- Creates Activity records in database
- Notifies via WebSocket when complete
```

#### Unlink OneLapFit Account
```
DELETE /api/v1/onelapfit/unlink
Authorization: Bearer <access_token>

Response:
{
  "detail": "OneLapFit unlinked successfully for user <user_id>"
}
```

### 4. Database Schema

Added field to `users_integrations` table:

```python
onelapfit_token: Mapped[str | None] = mapped_column(
    String(length=512),
    default=None,
    nullable=True,
    comment=("OneLapFit access token encrypted at rest with Fernet key"),
)
```

### 5. CRUD Operations

#### Link Account
```python
def link_onelapfit_account(
    user_integrations: UsersIntegrations,
    token: str,
    db: Session,
) -> None
```

#### Unlink Account
```python
def unlink_onelapfit_account(user_id: int, db: Session) -> None
```

### 6. Core Functions

#### Authentication
```python
async def login_onelapfit(email: str, password: str) -> str
```
- Takes email and password
- MD5 hashes the password
- Posts to OneLapFit login API
- Returns access token on success
- Raises HTTPException on failure

#### Activity Fetching
```python
async def fetch_onelapfit_activities(
    token: str,
    start_time: int,
    end_time: int,
    page: int = 0,
) -> dict
```
- Fetches riding list from OneLapFit
- Returns day-based activity data
- Supports pagination

#### Overview Fetching
```python
async def fetch_onelapfit_overview(
    token: str,
    start_time: int = 0,
    end_time: int = None,
) -> dict
```
- Returns summary data per day
- Provides time and activity count metrics

#### File Download
```python
async def download_fit_file(url: str) -> bytes
```
- Downloads FIT file from OneLapFit URL
- Returns file content as bytes

### 7. Activity Processing

The `activity_utils.py` module handles:

1. **Fetching** - Retrieves activities from OneLapFit API
2. **Parsing** - Converts OneLapFit data format to Activity model
3. **Storing** - Creates Activity records in database
4. **FIT Files** - Downloads and stores FIT files
5. **Notifications** - Sends WebSocket updates to frontend

#### Parsed Activity Fields
```python
{
    "name": str,                    # Activity name
    "type": str,                    # "Ride" for cycling
    "start_date": datetime,         # Activity start time (UTC)
    "elapsed_time": int,            # Total time in seconds
    "moving_time": int,             # Moving time in seconds
    "distance": float,              # Distance in km
    "elevation_gain": float,        # Elevation gain in meters
    "elevation_loss": float,        # Elevation loss (always 0 from OneLapFit)
    "avg_speed": float,             # Average speed in km/h
    "max_speed": float,             # Max speed (always 0 from OneLapFit)
    "avg_heartrate": int,           # Avg HR (always 0 from OneLapFit)
    "max_heartrate": int,           # Max HR (always 0 from OneLapFit)
    "avg_watts": float,             # Average power (AP field)
    "normalized_power": float,      # Normalized power (NP field)
    "ftp": float,                   # Functional threshold power
    "tss": float,                   # Training stress score
    "calories": int,                # Calories burned
    "kudos_count": int,             # Always 0 from OneLapFit
    "visibility": str,              # From user privacy settings
    "timezone": str,                # User's timezone
    "onelapfit_id": str,            # OneLapFit activity ID
}
```

### 8. Background Task Processing

Activities are fetched in background tasks to avoid blocking the API:

```
GET /api/v1/onelapfit/activities
  │
  └─► FastAPI return 202 Accepted
       │
       └─► Background Task:
            1. Decrypt token
            2. Call OneLapFit API
            3. Parse activities
            4. Create DB records
            5. Download FIT files
            6. Send WebSocket notification
```

## Frontend Implementation

### 1. Service Layer

**File:** `frontend/app/src/services/onelapfitService.js`

```javascript
export const onelapfit = {
  linkOneLapFit(email, password),          // Link account
  getOneLapFitActivitiesByDates(startDate, endDate), // Sync activities
  unlinkOneLapFit()                        // Unlink account
}
```

### 2. i18n Translations

**Location:** `frontend/app/src/i18n/[lang]/onelapfit/`

Supported languages:
- ca (Catalan)
- cn (Chinese)
- de (German)
- es (Spanish)
- fr (French)
- gl (Galician)
- it (Italian)
- nl (Dutch)
- pt (Portuguese)
- sl (Slovenian)
- sv (Swedish)
- tw (Traditional Chinese)
- us (English)

**Keys:**
```javascript
{
  "oneLapFitLinkTitle": "Link OneLapFit Account",
  "oneLapFitLinkDescription": "Connect your OneLapFit account...",
  "oneLapFitEmail": "Email",
  "oneLapFitPassword": "Password",
  "oneLapFitLink": "Link OneLapFit",
  "oneLapFitLinking": "Linking OneLapFit...",
  "oneLapFitLinked": "OneLapFit account linked successfully",
  "oneLapFitLinkError": "Failed to link OneLapFit account",
  "oneLapFitUnlink": "Unlink OneLapFit",
  "oneLapFitUnlinking": "Unlinking OneLapFit...",
  "oneLapFitUnlinked": "OneLapFit account unlinked successfully",
  "oneLapFitUnlinkError": "Failed to unlink OneLapFit account",
  "oneLapFitSyncActivities": "Sync OneLapFit Activities",
  "oneLapFitSyncing": "Syncing OneLapFit activities...",
  "oneLapFitSyncComplete": "OneLapFit activities synced successfully",
  "oneLapFitSyncError": "Failed to sync OneLapFit activities",
  "oneLapFitNotLinked": "OneLapFit not linked...",
  "oneLapFitInvalidCredentials": "Invalid OneLapFit credentials...",
  "oneLapFitConnectionError": "Unable to connect to OneLapFit service..."
}
```

### 3. Assets

**Logo:** `frontend/app/src/assets/onelapfit/onelapfit_logo.svg`

Logo registered in:
```typescript
// frontend/app/src/constants/integrationLogoConstants.ts
export const INTEGRATION_LOGOS = {
  strava: stravaLogo,
  garminConnectBadge: garminConnectBadge,
  garminConnectApp: garminConnectApp,
  oneLapFit: oneLapFitLogo
}
```

## OneLapFit API Reference

### Login Endpoint
```
POST https://rfs-fitness.rfsvr.com/api/v1/app/login
Content-Type: application/json

Body:
{
  "account": "email@example.com",
  "password": "<md5_hash_lowercase>"
}

Response:
{
  "code": 200,
  "error": "success",
  "data": {
    "token": "<access_token>"
  }
}
```

### Get Activities List
```
GET https://rfs-fitness.rfsvr.com/api/v1/app/record/riding/list?start_time=<unix_ts>&end_time=<unix_ts>&p=0&source=all&data_type=all

Headers:
Authorization: <token>

Response:
{
  "code": 200,
  "error": "success",
  "data": {
    "days": {
      "<unix_timestamp>": {
        "time": <seconds>,
        "date": <unix_timestamp>,
        "info": [
          {
            "type": 27,
            "time": <seconds>,
            "total_time": <seconds>,
            "start_time": <unix_timestamp>,
            "name": "Ride",
            "fileKey": "<fit_file_key>",
            "fileAddr": "<fit_file_url>",
            "did": "<device_id>",
            "cal": <calories>,
            "weight": <weight_kg>,
            "tag": "户外骑行",
            "avg_speed": 0,
            "elevation": <meters>,
            "TSS": <tss_score>,
            "FTP": <ftp_watts>,
            "AP": <avg_power>,
            "NP": <normalized_power>,
            "distance": <meters>
          }
        ]
      }
    },
    "page_count": 1
  }
}
```

### Get Riding Overview
```
GET https://rfs-fitness.rfsvr.com/api/v1/app/record/riding/overview?start_time=<unix_ts>&end_time=<unix_ts>&source=table&data_type=all

Headers:
Authorization: <token>

Response:
{
  "code": 200,
  "error": "success",
  "data": {
    "<unix_timestamp>": {
      "time": <seconds>,
      "count": <activity_count>
    },
    ...
  }
}
```

## Security Considerations

### 1. Token Storage
- OneLapFit tokens are encrypted with Fernet (symmetric encryption)
- Encrypted at rest in database
- Decrypted only when needed for API calls
- Never logged or displayed to users

### 2. Password Handling
- Passwords are hashed with MD5 before sending to OneLapFit API
- Passwords are NOT stored in database
- Only access tokens are saved (encrypted)
- If account is unlinked, all data is deleted

### 3. API Communication
- All requests to OneLapFit API use HTTPS
- Tokens included in Authorization header
- Rate limiting to be implemented per OneLapFit guidelines

## Error Handling

### Common Error Scenarios

1. **Invalid Credentials**
   - Response: 401 Unauthorized
   - Message: "OneLapFit login failed. Check your credentials."

2. **API Connection Error**
   - Response: 502 Bad Gateway
   - Message: "Unable to connect to OneLapFit service"

3. **Token Expired**
   - Response: 401 Unauthorized
   - Message: "OneLapFit token expired or invalid"

4. **No Activities Found**
   - Response: 200 OK with empty data
   - Message: "No new OneLapFit activities found"

## Testing the Integration

### 1. Link Account
```bash
curl -X PUT http://localhost:8000/api/v1/onelapfit/link \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your_token>" \
  -d '{
    "email": "user@example.com",
    "password": "password123"
  }'
```

### 2. Fetch Activities
```bash
curl -X GET "http://localhost:8000/api/v1/onelapfit/activities?start_date=2024-01-01T00:00:00&end_date=2024-12-31T23:59:59" \
  -H "Authorization: Bearer <your_token>"
```

### 3. Unlink Account
```bash
curl -X DELETE http://localhost:8000/api/v1/onelapfit/unlink \
  -H "Authorization: Bearer <your_token>"
```

## Future Enhancements

1. **Token Refresh** - Implement periodic token refresh if OneLapFit supports it
2. **Selective Sync** - Allow users to choose date ranges and activity types
3. **FIT File Storage** - Implement persistent FIT file storage and retrieval
4. **Gear Sync** - Sync device/bike information from OneLapFit (if available)
5. **Rate Limiting** - Implement smart rate limiting for API calls
6. **Webhook Support** - Real-time activity notifications if OneLapFit supports it
7. **Data Validation** - Enhanced validation of activity data from OneLapFit

## Integration Checklist

- [x] Backend module structure created
- [x] Database model updated
- [x] CRUD operations implemented
- [x] API endpoints implemented
- [x] Frontend service created
- [x] i18n translations added (13 languages)
- [x] Assets and logos created
- [x] Router registered
- [x] Error handling implemented
- [x] Activity parsing logic implemented
- [ ] Unit tests (recommended)
- [ ] Integration tests (recommended)
- [ ] API documentation (OpenAPI/Swagger)
- [ ] User documentation (recommended)

## Files Modified/Created

### Backend
- **Created:** `backend/app/onelapfit/__init__.py`
- **Created:** `backend/app/onelapfit/schema.py`
- **Created:** `backend/app/onelapfit/utils.py`
- **Created:** `backend/app/onelapfit/activity_utils.py`
- **Created:** `backend/app/onelapfit/router.py`
- **Modified:** `backend/app/users/users_integrations/models.py`
- **Modified:** `backend/app/users/users_integrations/crud.py`
- **Modified:** `backend/app/core/routes.py`

### Frontend
- **Created:** `frontend/app/src/services/onelapfitService.js`
- **Created:** `frontend/app/src/i18n/*/onelapfit/oneLapFitView.json` (13 files)
- **Created:** `frontend/app/src/assets/onelapfit/onelapfit_logo.svg`
- **Modified:** `frontend/app/src/constants/integrationLogoConstants.ts`

## Support and Maintenance

For issues or questions regarding the OneLapFit integration:
1. Check the error logs in the backend
2. Verify OneLapFit account credentials
3. Ensure OneLapFit API is accessible
4. Check database for stored token

## References

- OneLapFit API: https://rfs-fitness.rfsvr.com/api/v1/app
- Strava Integration (similar pattern): `backend/app/strava/`
- Garmin Integration (similar pattern): `backend/app/garmin/`
