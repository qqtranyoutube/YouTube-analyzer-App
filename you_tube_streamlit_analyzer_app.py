import os
import streamlit as st
from datetime import datetime, timedelta
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow

# ========================
# CONFIG
# ========================
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly"
]
CLIENT_SECRETS_FILE = "client_secrets.json"

# ========================
# AUTHENTICATION
# ========================
def get_authenticated_services():
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
    credentials = flow.run_local_server(port=0)

    youtube = build("youtube", "v3", credentials=credentials)
    youtube_analytics = build("youtubeAnalytics", "v2", credentials=credentials)
    return youtube, youtube_analytics

# ========================
# GET CHANNEL ID
# ========================
def get_channel_id(youtube):
    request = youtube.channels().list(
        part="id",
        mine=True
    )
    response = request.execute()
    return response["items"][0]["id"]

# ========================
# HELPER: GET RPM + MONETIZATION
# ========================
def get_videos_with_rpm_and_monetization(youtube, youtube_analytics, channel_id, video_ids):
    video_response = youtube.videos().list(
        part="snippet,status",
        id=",".join(video_ids)
    ).execute()

    results = []
    for item in video_response.get("items", []):
        vid = item["id"]
        title = item["snippet"]["title"]
        published = item["snippet"]["publishedAt"]
        monetization_status = item["status"].get("monetizationStatus", "unknown")

        rpm_value = None
        try:
            analytics_response = youtube_analytics.reports().query(
                ids=f"channel=={channel_id}",
                startDate=published[:10],
                endDate=datetime.utcnow().strftime("%Y-%m-%d"),
                metrics="estimatedRevenue,views",
                filters=f"video=={vid}"
            ).execute()

            rows = analytics_response.get("rows", [])
            if rows:
                revenue, views = rows[0]
                rpm_value = round((revenue / views) * 1000, 2) if views > 0 else 0
        except Exception:
            rpm_value = "N/A"

        results.append({
            "Video ID": vid,
            "Title": title,
            "Published": published,
            "Monetization": monetization_status,
            "RPM": rpm_value
        })

    return results

# ========================
# FETCH RECENT VIDEOS (last X days)
# ========================
def fetch_recent_videos_with_stats(youtube, youtube_analytics, channel_id, days=7):
    search_response = youtube.search().list(
        part="id",
        channelId=channel_id,
        order="date",
        maxResults=10,
        publishedAfter=(datetime.utcnow() - timedelta(days=days)).isoformat("T") + "Z",
        type="video"
    ).execute()

    video_ids = [item["id"]["videoId"] for item in search_response.get("items", [])]
    if not video_ids:
        return []

    return get_videos_with_rpm_and_monetization(youtube, youtube_analytics, channel_id, video_ids)

# ========================
# FETCH TODAY VIDEOS + LIVESTREAMS
# ========================
def fetch_today_videos_with_stats(youtube, youtube_analytics, channel_id):
    today_iso = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat("T") + "Z"
    search_response = youtube.search().list(
        part="id",
        channelId=channel_id,
        order="date",
        maxResults=15,
        publishedAfter=today_iso,
        type="video"
    ).execute()

    # Also check for livestreams
    live_response = youtube.search().list(
        part="id",
        channelId=channel_id,
        eventType="live",
        type="video",
        maxResults=5
    ).execute()

    video_ids = [item["id"]["videoId"] for item in search_response.get("items", [])]

    for live_item in live_response.get("items", []):
        vid_id = live_item["id"]["videoId"]
        if vid_id not in video_ids:
            video_ids.append(vid_id)

    if not video_ids:
        return []

    return get_videos_with_rpm_and_monetization(youtube, youtube_analytics, channel_id, video_ids)

# ========================
# STREAMLIT UI
# ========================
def main():
    st.set_page_config(page_title="YouTube Channel Analyzer", layout="wide")
    st.title("📊 YouTube Channel Analyzer PRO")

    st.write("Authenticating with YouTube...")
    youtube, youtube_analytics = get_authenticated_services()
    channel_id = get_channel_id(youtube)

    # Recent Videos Section
    st.subheader("Recent Videos (With RPM & Monetization)")
    data_recent = fetch_recent_videos_with_stats(youtube, youtube_analytics, channel_id, days=7)
    if data_recent:
        st.table(data_recent)
    else:
        st.warning("No recent videos found in the past week.")

    # Today / Realtime Videos Section
    st.subheader("Realtime / Today Feeds (Videos + Livestreams, With RPM & Monetization)")
    data_today = fetch_today_videos_with_stats(youtube, youtube_analytics, channel_id)
    if data_today:
        st.table(data_today)
    else:
        st.warning("No videos or livestreams found for today.")

if __name__ == "__main__":
    main()
