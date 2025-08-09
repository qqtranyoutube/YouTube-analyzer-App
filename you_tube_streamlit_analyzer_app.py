import streamlit as st
from datetime import datetime, timedelta, timezone
import pandas as pd
import matplotlib.pyplot as plt
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
import os

# --------------------- CONFIG & SCOPE ---------------------
SCOPES = [
    'https://www.googleapis.com/auth/youtube.readonly',
    'https://www.googleapis.com/auth/yt-analytics.readonly'
]

st.set_page_config(page_title="YouTube Realtime & Analytics Analyzer", layout='wide')
st.title('YouTube Realtime & Analytics Analyzer')

# --------------------- SIDEBAR ---------------------
st.sidebar.header('Settings & Credentials')

api_key = st.sidebar.text_input('YouTube Data API Key (optional)', value='')
use_oauth = st.sidebar.checkbox('Use OAuth for analytics (recommended)', value=False)
client_secrets_path = st.sidebar.text_input('OAuth client_secrets.json path (if using OAuth)', value='client_secrets.json')
channel_id_input = st.sidebar.text_input('Channel ID (required if not using OAuth)', value='')
max_results = st.sidebar.slider('Max results per search', min_value=5, max_value=50, value=10)

# --------------------- HELPERS ---------------------
def default_published_after():
    return (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

@st.cache_resource
def build_youtube(api_key=None, credentials=None):
    if credentials:
        return build('youtube', 'v3', credentials=credentials)
    elif api_key:
        return build('youtube', 'v3', developerKey=api_key)
    else:
        return None

def run_oauth_flow(client_secrets_file):
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, SCOPES)
    creds = flow.run_local_server(port=0)
    return creds

def fetch_video_analytics(analytics, channel_id, video_ids):
    """Fetch estimated revenue & views for specific videos and compute RPM."""
    try:
        res = analytics.reports().query(
            ids='channel==' + channel_id,
            startDate=(datetime.now() - timedelta(days=7)).date().isoformat(),
            endDate=datetime.now().date().isoformat(),
            metrics='estimatedRevenue,views',
            dimensions='video',
            filters='video==' + ','.join(video_ids)
        ).execute()

        data = {}
        for row in res.get('rows', []):
            vid_id = row[0]
            revenue = float(row[1]) if row[1] is not None else 0.0
            views = int(row[2]) if row[2] is not None else 0
            rpm = (revenue / views * 1000) if views > 0 else 0
            data[vid_id] = {'estimatedRevenue': revenue, 'rpm': rpm}
        return data
    except HttpError as e:
        st.error(f"Error fetching video analytics: {e}")
        return {}

def fetch_recent_videos(youtube, published_after_iso, max_results=10, analytics=None, channel_id=None):
    try:
        search_response = youtube.search().list(
            part='snippet',
            type='video',
            order='date',
            publishedAfter=published_after_iso,
            maxResults=max_results,
            q='a'  # broad search
        ).execute()

        video_ids = [item['id']['videoId'] for item in search_response.get('items', []) if 'videoId' in item['id']]

        if not video_ids:
            return []

        videos_response = youtube.videos().list(
            part='snippet,statistics,status',
            id=','.join(video_ids)
        ).execute()

        vids = []
        monetization_data = {}
        analytics_data = {}

        # If OAuth and analytics provided, fetch revenue/RPM
        if analytics and channel_id:
            analytics_data = fetch_video_analytics(analytics, channel_id, video_ids)

        for video in videos_response.get('items', []):
            vid_id = video['id']
            stats = video.get('statistics', {})
            monet_status = video.get('status', {}).get('monetizationStatus', 'unknown')

            row = {
                'videoId': vid_id,
                'title': video['snippet']['title'],
                'publishedAt': video['snippet']['publishedAt'],
                'viewCount': int(stats.get('viewCount', 0)),
                'likeCount': int(stats.get('likeCount', 0)) if 'likeCount' in stats else None,
                'commentCount': int(stats.get('commentCount', 0)) if 'commentCount' in stats else None,
                'monetization': monet_status
            }

            if vid_id in analytics_data:
                row['estimatedRevenue'] = analytics_data[vid_id]['estimatedRevenue']
                row['rpm'] = analytics_data[vid_id]['rpm']
            else:
                row['estimatedRevenue'] = None
                row['rpm'] = None

            vids.append(row)
        return vids
    except HttpError as e:
        if e.resp.status == 403:
            st.error("YouTube API quota exceeded or access denied.")
        else:
            st.error(f"YouTube API error: {e}")
        return []

def get_channel_stats(youtube, credentials=None, channel_id=None):
    try:
        if credentials:
            res = youtube.channels().list(part='snippet,statistics,status,contentDetails', mine=True).execute()
        else:
            if channel_id:
                res = youtube.channels().list(part='snippet,statistics,status,contentDetails', id=channel_id).execute()
            else:
                st.error('Please provide Channel ID if not using OAuth.')
                return None
        items = res.get('items', [])
        if not items:
            return None
        ch = items[0]
        return {
            'id': ch['id'],
            'title': ch['snippet'].get('title'),
            'uploadsPlaylistId': ch.get('contentDetails', {}).get('relatedPlaylists', {}).get('uploads'),
            'subscribers': int(ch.get('statistics', {}).get('subscriberCount', 0)),
            'views': int(ch.get('statistics', {}).get('viewCount', 0)),
            'videos': int(ch.get('statistics', {}).get('videoCount', 0)),
            'status': ch.get('status', {})
        }
    except HttpError as e:
        st.error(f'Error fetching channel info: {e}')
        return None

# --------------------- MAIN APP ---------------------
def main():
    credentials = None
    analytics = None
    if use_oauth:
        if os.path.exists(client_secrets_path):
            if st.sidebar.button('Connect via OAuth'):
                creds = run_oauth_flow(client_secrets_path)
                credentials = creds
                st.success('OAuth connected.')
                analytics = build('youtubeAnalytics', 'v2', credentials=creds)
        else:
            st.sidebar.warning('OAuth client_secrets.json not found.')

    yt = None
    if use_oauth and credentials:
        yt = build_youtube(credentials=credentials)
    elif api_key:
        yt = build_youtube(api_key=api_key)

    col1, col2 = st.columns([2, 1])

    with col1:
        st.header('Realtime / Today Feeds')
        if yt:
            if st.button('Fetch videos published recently'):
                start_iso = default_published_after()

                ch_info = None
                if use_oauth and credentials:
                    ch_info = get_channel_stats(yt, credentials=credentials)
                    channel_id_for_analytics = ch_info['id'] if ch_info else None
                else:
                    channel_id_for_analytics = None

                videos = fetch_recent_videos(
                    yt,
                    start_iso,
                    max_results=max_results,
                    analytics=analytics if use_oauth else None,
                    channel_id=channel_id_for_analytics
                )

                if not videos:
                    st.info('No videos found.')
                else:
                    df = pd.DataFrame(videos)
                    df['publishedAt'] = pd.to_datetime(df['publishedAt'])
                    df = df.sort_values('publishedAt')

                    st.subheader('Recent Videos')
                    st.dataframe(df[['title', 'videoId', 'publishedAt', 'viewCount', 'estimatedRevenue', 'rpm', 'monetization']])

                    popular = df[df['viewCount'] >= 1000]
                    if not popular.empty:
                        st.subheader('Videos with ≥ 1000 views')
                        st.dataframe(popular[['title', 'videoId', 'publishedAt', 'viewCount', 'estimatedRevenue', 'rpm', 'monetization']])
                    else:
                        st.info('No videos reached 1000 views yet.')
        else:
            st.info('API client not ready.')

    with col2:
        st.header('Channel Statistics')
        if yt:
            if st.button('Get channel stats'):
                ch_stats = get_channel_stats(yt, credentials=credentials if use_oauth else None, channel_id=channel_id_input if not use_oauth else None)
                if ch_stats:
                    st.metric('Subscribers', ch_stats['subscribers'])
                    st.metric('Total Views', ch_stats['views'])
                    st.metric('Total Videos', ch_stats['videos'])
                    st.json(ch_stats['status'])
        else:
            st.info('API client not ready.')

    # Keep your analytics section untouched except it's already doing RPM there

if __name__ == "__main__":
    main()
