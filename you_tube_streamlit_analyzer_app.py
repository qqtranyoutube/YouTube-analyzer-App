import streamlit as st
from datetime import datetime, timedelta, timezone
import time
import pandas as pd
import matplotlib.pyplot as plt
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
import os

# ---------- Config
SCOPES = [
    'https://www.googleapis.com/auth/youtube.readonly',
    'https://www.googleapis.com/auth/yt-analytics.readonly'
]

st.set_page_config(page_title="YouTube Realtime Analyzer", layout='wide')
st.title('YouTube Realtime & Daily Analyzer')

# Sidebar: credentials & options + channel ID input
st.sidebar.header('Credentials & Options')
api_key = st.sidebar.text_input('YouTube Data API Key (optional)', value='')
use_oauth = st.sidebar.checkbox('Use OAuth (recommended for RPM/Analytics)', value=False)
client_secrets_path = st.sidebar.text_input('Path to client_secrets.json (if using OAuth)', value='client_secrets.json')
poll_interval = st.sidebar.number_input('Auto-refresh poll interval (seconds)', min_value=15, max_value=3600, value=60)
max_results = st.sidebar.slider('Search max results per call', min_value=5, max_value=50, value=10)

# New: channel ID input for public data (required if no OAuth)
channel_id_input = st.sidebar.text_input('Channel ID (required if not using OAuth)', value='')

# Helper: ISO timestamp for 3 days ago in UTC (wider window)
def default_published_after():
    return (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

# Build YouTube API client (Data API)
@st.cache_resource
def build_youtube(api_key: str = None, credentials: Credentials = None):
    if credentials:
        return build('youtube', 'v3', credentials=credentials)
    elif api_key:
        return build('youtube', 'v3', developerKey=api_key)
    else:
        return None

# OAuth flow
def run_oauth_flow(client_secrets_file: str):
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, SCOPES)
    creds = flow.run_local_server(port=0)
    return creds

# Get videos published recently with generic query q='a' and debug
def fetch_videos_published_today(youtube, published_after_iso, max_results=10):
    try:
        req = youtube.search().list(
            part='snippet',
            type='video',
            order='date',
            publishedAfter=published_after_iso,
            maxResults=max_results,
            q='a'  # generic query to get more results
        )
        res = req.execute()
        # DEBUG: Show raw search response
        st.write("Debug: Raw search response", res)

        video_ids = [item['id']['videoId'] for item in res.get('items', []) if 'videoId' in item['id']]
        # DEBUG: Show extracted video IDs
        st.write(f"Debug: Video IDs found ({len(video_ids)}):", video_ids)

        if not video_ids:
            return []
        vids = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i+50]
            vr = youtube.videos().list(part='snippet,statistics,contentDetails', id=','.join(batch)).execute()
            for v in vr.get('items', []):
                snippet = v.get('snippet', {})
                stats = v.get('statistics', {})
                published_at = snippet.get('publishedAt')
                title = snippet.get('title')
                view_count = int(stats.get('viewCount', 0))
                vids.append({
                    'videoId': v['id'],
                    'title': title,
                    'publishedAt': published_at,
                    'viewCount': view_count,
                    'likeCount': int(stats.get('likeCount', 0)) if stats.get('likeCount') else None,
                    'commentCount': int(stats.get('commentCount', 0)) if stats.get('commentCount') else None
                })
        return vids
    except HttpError as e:
        if e.resp.status == 403:
            st.error("YouTube API quota exceeded. Please wait or reduce polling frequency.")
        else:
            st.error(f'YouTube API error: {e}')
        return []

# Get channel stats (updated to handle OAuth vs API key + channel id)
def get_channel_stats(youtube, credentials=None, channel_id=None):
    try:
        if credentials:
            # OAuth present → use mine=True to get authorized user's channel
            res = youtube.channels().list(part='snippet,statistics,status,contentDetails', mine=True).execute()
        else:
            if channel_id:
                # API key only, must specify channel id
                res = youtube.channels().list(part='snippet,statistics,status,contentDetails', id=channel_id).execute()
            else:
                st.error('Channel ID must be provided in the sidebar if not using OAuth.')
                return None
        items = res.get('items', [])
        if not items:
            st.warning('No channel data found.')
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

# Analytics: fetch estimatedRevenue and views for date range
def fetch_analytics(analytics, channel_id, start_date, end_date):
    try:
        res = analytics.reports().query(
            ids='channel==' + channel_id,
            startDate=start_date,
            endDate=end_date,
            metrics='estimatedRevenue,views',
            dimensions='day'
        ).execute()
        cols = [h['name'] for h in res.get('columnHeaders', [])]
        rows = res.get('rows', [])
        data = []
        for r in rows:
            day = r[0]
            revenue = float(r[1]) if r[1] is not None else 0.0
            views = int(r[2]) if r[2] is not None else 0
            data.append({'date': day, 'estimatedRevenue': revenue, 'views': views})
        return pd.DataFrame(data)
    except HttpError as e:
        st.error(f'Analytics API error: {e}')
        return pd.DataFrame()

# Compute RPM
def compute_rpm(df):
    df = df.copy()
    df['rpm'] = df.apply(lambda r: (r['estimatedRevenue'] / r['views'] * 1000) if r['views'] > 0 else 0, axis=1)
    return df

# UI: Credentials
credentials = None
analytics = None
if use_oauth:
    if os.path.exists(client_secrets_path):
        if st.sidebar.button('Connect via OAuth'):
            creds = run_oauth_flow(client_secrets_path)
            credentials = creds
            st.success('OAuth connected — use the controls to fetch analytics.')
            analytics = build('youtubeAnalytics', 'v2', credentials=creds)
    else:
        st.sidebar.warning('client_secrets.json not found at the path provided.')

# Build YouTube client (Data API)
yt = None
if use_oauth and credentials:
    yt = build_youtube(credentials=credentials)
elif api_key:
    yt = build_youtube(api_key=api_key)
else:
    st.warning('Provide an API key or enable OAuth to use the Data API features.')

# Main controls
col1, col2 = st.columns([2, 1])
with col1:
    st.header('Realtime / Today Feeds')
    if yt:
        if st.button('Fetch videos published today'):
            # Use 3 days ago for publishedAfter (wider window)
            start_iso = default_published_after()
            vids = fetch_videos_published_today(yt, start_iso, max_results=max_results)
            if not vids:
                st.info('No videos found for today (or API returned none).')
            else:
                df = pd.DataFrame(vids)
                df['publishedAt'] = pd.to_datetime(df['publishedAt'])
                df = df.sort_values('publishedAt')
                st.subheader('All videos published recently')
                st.dataframe(df[['title', 'videoId', 'publishedAt', 'viewCount']])

                reached = df[df['viewCount'] >= 1000].copy()
                if not reached.empty:
                    reached = reached.sort_values('publishedAt')
                    st.subheader('Videos published recently that have >= 1000 views (earliest published first)')
                    st.dataframe(reached[['title', 'videoId', 'publishedAt', 'viewCount']])
                else:
                    st.info('No videos published recently have reached 1000+ views yet.')
    else:
        st.info('API client not ready. Provide API key or OAuth.')

with col2:
    st.header('Channel / Analytics')
    if yt:
        if st.button('Get channel statistics'):
            ch = get_channel_stats(yt, credentials=credentials if use_oauth else None, channel_id=channel_id_input if not use_oauth else None)
            if ch:
                st.metric('Subscribers', f"{ch['subscribers']}")
                st.metric('Channel Views', f"{ch['views']}")
                st.metric('Total Videos', f"{ch['videos']}")
                st.write('Channel status (API raw):')
                st.json(ch['status'])
                st.write('If monetization details are not available here, you must check YouTube Studio or request owner-level Analytics access.')
    else:
        st.info('API client not ready.')

# Analytics charts
st.header('Views & Estimated Revenue (Analytics)')
with st.expander('Analytics chart controls (requires OAuth & analytics permission)'):
    channel_id_analytics = st.text_input('Channel ID (leave empty to use authorized channel)', value='')
    end_date = st.date_input('End date', value=datetime.now().date())
    start_date = st.date_input('Start date', value=(datetime.now() - timedelta(days=7)).date())
    if analytics is None and use_oauth and credentials:
        analytics = build('youtubeAnalytics', 'v2', credentials=credentials)
    if st.button('Fetch analytics'):
        ch = None
        if not channel_id_analytics:
            chinfo = get_channel_stats(yt, credentials=credentials if use_oauth else None, channel_id=channel_id_input if not use_oauth else None)
            if chinfo:
                channel_id = chinfo['id']
            else:
                st.error('Unable to determine authorized channel id. Provide channel id manually.')
                channel_id = None
        else:
            channel_id = channel_id_analytics
        if channel_id and analytics:
            df = fetch_analytics(analytics, channel_id, start_date.isoformat(), end_date.isoformat())
            if df.empty:
                st.info('No analytics rows returned. Check permissions and date range.')
            else:
                df = compute_rpm(df)
                st.dataframe(df)
                fig1 = plt.figure()
                plt.plot(pd.to_datetime(df['date']), df['views'])
                plt.title('Daily Views')
                plt.xlabel('Date')
                plt.ylabel('Views')
                st.pyplot(fig1)

                fig2 = plt.figure()
                plt.plot(pd.to_datetime(df['date']), df['estimatedRevenue'])
                plt.title('Estimated Revenue (local currency in Analytics)')
                plt.xlabel('Date')
                plt.ylabel('Revenue')
                st.pyplot(fig2)

                fig3 = plt.figure()
                plt.plot(pd.to_datetime(df['date']), df['rpm'])
                plt.title('RPM (estimated)')
                plt.xlabel('Date')
                plt.ylabel('RPM')
                st.pyplot(fig3)
        else:
            st.error('Analytics client not available or channel id missing. Ensure OAuth is connected and you granted analytics scope.')

# Footer / instructions
with st.expander("Show Deployment & Accuracy Notes"):
    st.markdown("""
    ### Deployment & GitHub
    - Put this file in a Git repository along with `requirements.txt` and your `client_secrets.json` (keep secrets out of public repos!).
    - On Streamlit Cloud: connect the GitHub repo and set required secrets as environment variables (e.g., `GOOGLE_CLIENT_SECRETS`) or use an OAuth flow locally.
    - Consider caching results and using a backend worker (Cloud Run / AWS Lambda) to poll frequently to avoid hitting the Data API quota.

    ### About Accuracy & Limitations
    - **\"Fastest to 1000 views\"** is approximated by checking which videos published earlier already have ≥1000 views. To measure exact time-to-1k, you must poll frequently and record timestamps when each video crosses thresholds.
    - **RPM (Revenue per mille)** is estimated from the Analytics API `estimatedRevenue`. It may not match final payments and depends on currency & sampling.
    """)

st.success('App ready. Use the controls above; full instructions are in the code comments.')
