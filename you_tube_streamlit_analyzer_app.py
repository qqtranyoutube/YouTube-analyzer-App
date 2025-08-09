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

def fetch_recent_videos(youtube, published_after_iso, max_results=10):
    try:
        search_response = youtube.search().list(
            part='snippet',
            type='video',
            order='date',
            publishedAfter=published_after_iso,
            maxResults=max_results,
            q='a'  # generic to get broad results
        ).execute()

        video_ids = [item['id']['videoId'] for item in search_response.get('items', []) if 'videoId' in item['id']]
        st.write(f"Debug: Video IDs found: {video_ids}")

        if not video_ids:
            return []

        videos_response = youtube.videos().list(
            part='snippet,statistics',
            id=','.join(video_ids)
        ).execute()

        vids = []
        for video in videos_response.get('items', []):
            vids.append({
                'videoId': video['id'],
                'title': video['snippet']['title'],
                'publishedAt': video['snippet']['publishedAt'],
                'viewCount': int(video['statistics'].get('viewCount', 0)),
                'likeCount': int(video['statistics'].get('likeCount', 0)) if 'likeCount' in video['statistics'] else None,
                'commentCount': int(video['statistics'].get('commentCount', 0)) if 'commentCount' in video['statistics'] else None,
            })
        return vids
    except HttpError as e:
        if e.resp.status == 403:
            st.error("YouTube API quota exceeded or access denied. Reduce polling or check credentials.")
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

def fetch_analytics(analytics, channel_id, start_date, end_date):
    try:
        res = analytics.reports().query(
            ids='channel==' + channel_id,
            startDate=start_date,
            endDate=end_date,
            metrics='estimatedRevenue,views',
            dimensions='day'
        ).execute()
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

def compute_rpm(df):
    df = df.copy()
    df['rpm'] = df.apply(lambda r: (r['estimatedRevenue'] / r['views'] * 1000) if r['views'] > 0 else 0, axis=1)
    return df

def get_monetization_status(youtube, credentials):
    try:
        res = youtube.channels().list(part='status', mine=True).execute()
        items = res.get('items', [])
        if not items:
            st.warning("No channel status info found.")
            return None
        status = items[0].get('status', {})
        return status
    except HttpError as e:
        st.error(f"Error fetching monetization status: {e}")
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
                st.success('OAuth connected. Ready for analytics.')
                analytics = build('youtubeAnalytics', 'v2', credentials=creds)
        else:
            st.sidebar.warning('OAuth client_secrets.json not found at the given path.')

    yt = None
    if use_oauth and credentials:
        yt = build_youtube(credentials=credentials)
    elif api_key:
        yt = build_youtube(api_key=api_key)
    else:
        st.warning('Please provide an API key or enable OAuth.')

    col1, col2 = st.columns([2, 1])

    with col1:
        st.header('Realtime / Today Feeds')
        if yt:
            if st.button('Fetch videos published recently'):
                start_iso = default_published_after()
                videos = fetch_recent_videos(yt, start_iso, max_results=max_results)
                if not videos:
                    st.info('No videos found for recent period.')
                else:
                    df = pd.DataFrame(videos)
                    df['publishedAt'] = pd.to_datetime(df['publishedAt'])
                    df = df.sort_values('publishedAt')
                    st.subheader('Recent Videos')
                    st.dataframe(df[['title', 'videoId', 'publishedAt', 'viewCount']])

                    popular = df[df['viewCount'] >= 1000]
                    if not popular.empty:
                        popular = popular.sort_values('publishedAt')
                        st.subheader('Videos with ≥ 1000 views')
                        st.dataframe(popular[['title', 'videoId', 'publishedAt', 'viewCount']])
                    else:
                        st.info('No videos have reached 1000 views yet.')
        else:
            st.info('API client not ready. Provide API key or OAuth credentials.')

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

                    if use_oauth and credentials:
                        monet_status = get_monetization_status(yt, credentials)
                        if monet_status:
                            st.subheader('Monetization Status')
                            st.json(monet_status)
                else:
                    st.info('No channel stats available.')
        else:
            st.info('API client not ready.')

    st.header('YouTube Analytics (Estimated Revenue & Views)')

    with st.expander('Analytics Chart Controls (requires OAuth)'):
        channel_id_analytics = st.text_input('Channel ID for Analytics (leave empty to use authorized channel)', value='')
        start_date = st.date_input('Start date', value=(datetime.now() - timedelta(days=7)).date())
        end_date = st.date_input('End date', value=datetime.now().date())

        if analytics is None and use_oauth and credentials:
            analytics = build('youtubeAnalytics', 'v2', credentials=credentials)

        if st.button('Fetch Analytics'):
            ch_info = None
            if not channel_id_analytics:
                ch_info = get_channel_stats(yt, credentials=credentials if use_oauth else None, channel_id=channel_id_input if not use_oauth else None)
                channel_id_for_analytics = ch_info['id'] if ch_info else None
            else:
                channel_id_for_analytics = channel_id_analytics

            if channel_id_for_analytics and analytics:
                df = fetch_analytics(analytics, channel_id_for_analytics, start_date.isoformat(), end_date.isoformat())
                if df.empty:
                    st.info('No analytics data found for the given period.')
                else:
                    df = compute_rpm(df)
                    st.dataframe(df)

                    avg_rpm = df['rpm'].mean()
                    st.metric(f"Average RPM ({start_date} to {end_date})", f"${avg_rpm:.2f}")

                    fig_views, ax1 = plt.subplots()
                    ax1.plot(pd.to_datetime(df['date']), df['views'], label='Views')
                    ax1.set_xlabel('Date')
                    ax1.set_ylabel('Views')
                    ax1.set_title('Daily Views')
                    st.pyplot(fig_views)

                    fig_rev, ax2 = plt.subplots()
                    ax2.plot(pd.to_datetime(df['date']), df['estimatedRevenue'], label='Estimated Revenue', color='green')
                    ax2.set_xlabel('Date')
                    ax2.set_ylabel('Estimated Revenue')
                    ax2.set_title('Estimated Revenue')
                    st.pyplot(fig_rev)

                    fig_rpm, ax3 = plt.subplots()
                    ax3.plot(pd.to_datetime(df['date']), df['rpm'], label='RPM', color='orange')
                    ax3.set_xlabel('Date')
                    ax3.set_ylabel('RPM')
                    ax3.set_title('RPM (Revenue per Mille)')
                    st.pyplot(fig_rpm)
            else:
                st.error('Analytics client or channel ID missing or not authorized.')

    with st.expander("Deployment & Accuracy Notes"):
        st.markdown("""
        ### Deployment & GitHub
        - Put this file in a Git repository along with `requirements.txt` and your `client_secrets.json` (keep secrets out of public repos!).
        - On Streamlit Cloud: connect the GitHub repo and set required secrets as environment variables (e.g., `GOOGLE_CLIENT_SECRETS`) or use OAuth locally.
        - Cache results and/or use a backend worker (Cloud Run / AWS Lambda) to avoid hitting YouTube Data API quotas frequently.

        ### Accuracy & Limitations
        - "Fastest to 1000 views" is approximate by checking videos that already have >= 1000 views.
        - RPM (Revenue per mille) is estimated from Analytics API estimatedRevenue; actual payouts may differ.
        - Monetization status info shown here is limited and only available for the authenticated channel.
        """)

    st.success('App ready. Use sidebar and buttons above to fetch data.')

if __name__ == "__main__":
    main()
