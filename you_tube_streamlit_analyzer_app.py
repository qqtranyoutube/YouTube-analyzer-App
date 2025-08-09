import streamlit as st
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import pandas as pd

# --- Config ---
API_KEY = st.secrets["YOUTUBE_API_KEY"] if "YOUTUBE_API_KEY" in st.secrets else st.text_input("Enter YouTube API Key")
MAX_RESULTS = 10

def get_youtube_client(api_key):
    return build("youtube", "v3", developerKey=api_key)

def fetch_recent_videos(youtube, published_after_iso, max_results=10):
    try:
        # Search videos with generic query to get results
        search_response = youtube.search().list(
            part="snippet",
            type="video",
            order="date",
            publishedAfter=published_after_iso,
            maxResults=max_results,
            q="a"  # generic query to get broad results
        ).execute()

        video_ids = [item["id"]["videoId"] for item in search_response.get("items", []) if "videoId" in item["id"]]

        if not video_ids:
            st.info("No videos found in search response.")
            return pd.DataFrame()

        # Get video details including stats (viewCount etc)
        videos_response = youtube.videos().list(
            part="snippet,statistics",
            id=",".join(video_ids)
        ).execute()

        videos_data = []
        for video in videos_response.get("items", []):
            videos_data.append({
                "videoId": video["id"],
                "title": video["snippet"]["title"],
                "publishedAt": video["snippet"]["publishedAt"],
                "viewCount": int(video["statistics"].get("viewCount", 0)),
                "likeCount": int(video["statistics"].get("likeCount", 0)),
                "commentCount": int(video["statistics"].get("commentCount", 0)),
            })

        return pd.DataFrame(videos_data)

    except HttpError as e:
        st.error(f"YouTube API error: {e}")
        return pd.DataFrame()

def main():
    st.title("YouTube Recent Videos Fetcher")

    if not API_KEY:
        st.warning("Please enter your YouTube Data API key.")
        return

    youtube = get_youtube_client(API_KEY)

    # Default to 3 days ago UTC
    published_after = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

    if st.button("Fetch Recent Videos"):
        df = fetch_recent_videos(youtube, published_after, MAX_RESULTS)
        if df.empty:
            st.info("No recent videos found.")
        else:
            st.write(f"Videos published after {published_after}")
            st.dataframe(df)

if __name__ == "__main__":
    main()
