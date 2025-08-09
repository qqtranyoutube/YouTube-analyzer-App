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
poll_interval = st.sidebar.number_input('Poll interval seconds (if using auto-refresh)', min_value=15, max_value=3600, value=60)

# --------------------- HELPERS / BUILDERS ---------------------
def default_published_after():
    # 3 days ago (UTC) default window
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

# --------------------- ANALYTICS HELPERS ---------------------
def fetch_video_analytics_map(analytics, channel_id, video_ids, lookback_days=7):
    """
    Returns dict {videoId: {'estimatedRevenue': float, 'views': int, 'rpm': float}}
    Uses Analytics API with dimensions=video and filter video==id1,id2...
    """
    if not analytics or not channel_id or not video_ids:
        return {}

    try:
        start_date = (datetime.now().date() - timedelta(days=lookback_days)).isoformat()
        end_date = datetime.now().date().isoformat()

        # Analytics API: query by video dimension
        res = analytics.reports().query(
            ids='channel==' + channel_id,
            startDate=start_date,
            endDate=end_date,
            metrics='estimatedRevenue,views',
            dimensions='video',
            filters='video==' + ','.join(video_ids)
        ).execute()

        rows = res.get('rows', []) or []
        mapping = {}
        for r in rows:
            # r example: [videoId, estimatedRevenue, views]
            vid = r[0]
            revenue = float(r[1]) if r[1] is not None else 0.0
            views = int(r[2]) if r[2] is not None else 0
            rpm = (revenue / views * 1000) if views > 0 else 0
            mapping[vid] = {'estimatedRevenue': revenue, 'views': views, 'rpm': rpm}
        return mapping
    except HttpError as e:
        st.warning(f"Analytics API error fetching per-video analytics: {e}")
        return {}

# --------------------- FETCH VIDEOS + MONETIZATION + RPM ---------------------
def fetch_recent_videos_full(youtube, published_after_iso, max_results=10, analytics=None, channel_id_for_analytics=None):
    """
    Fetch recent videos (search), then videos.list(part=snippet,statistics,status) to get monetization,
    then optionally call analytics to get estimatedRevenue & rpm per video.
    Returns list of dict rows.
    """
    try:
        search_response = youtube.search().list(
            part='snippet',
            type='video',
            order='date',
            publishedAfter=published_after_iso,
            maxResults=max_results,
            q='a'  # broad query to increase results
        ).execute()
        # debug raw response is helpful
        st.debug("Raw search response:", search_response)

        video_ids = [item['id']['videoId'] for item in search_response.get('items', []) if 'videoId' in item['id']]
        st.write(f"Debug: Video IDs found ({len(video_ids)}): {video_ids}")

        if len(video_ids) == 0:
            return []

        # Get per-video details including monetization status
        vids_resp = youtube.videos().list(part='snippet,statistics,status', id=','.join(video_ids)).execute()

        # If analytics available, get revenue/views -> rpm for these video ids
        analytics_map = {}
        if analytics and channel_id_for_analytics:
            analytics_map = fetch_video_analytics_map(analytics, channel_id_for_analytics, video_ids, lookback_days=7)

        rows = []
        for v in vids_resp.get('items', []):
            vid = v['id']
            snippet = v.get('snippet', {})
            stats = v.get('statistics', {})
            status = v.get('status', {})

            view_count = int(stats.get('viewCount', 0))
            monetization = status.get('monetizationStatus') if status else None

            estimatedRevenue = analytics_map.get(vid, {}).get('estimatedRevenue') if analytics_map else None
            rpm = analytics_map.get(vid, {}).get('rpm') if analytics_map else None

            rows.append({
                'videoId': vid,
                'title': snippet.get('title'),
                'publishedAt': snippet.get('publishedAt'),
                'viewCount': view_count,
                'likeCount': int(stats.get('likeCount', 0)) if stats.get('likeCount') else None,
                'commentCount': int(stats.get('commentCount', 0)) if stats.get('commentCount') else None,
                'monetization': monetization,
                'estimatedRevenue': estimatedRevenue,
                'rpm': rpm
            })
        return rows
    except HttpError as e:
        if hasattr(e, 'resp') and e.resp.status == 403:
            st.error("YouTube API quota exceeded or access denied.")
        else:
            st.error(f"YouTube API error: {e}")
        return []

def fetch_today_videos_full(youtube, analytics=None, channel_id_for_analytics=None, max_results_today=50):
    """
    Fetch videos published since UTC midnight today AND current live streams (if any),
    then enrich with monetization + analytics (rpm).
    """
    try:
        today_iso = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + 'Z'

        search_today = youtube.search().list(
            part='snippet',
            type='video',
            order='date',
            publishedAfter=today_iso,
            maxResults=max_results_today,
            q='a'
        ).execute()

        # also include live videos (currently live)
        live_search = youtube.search().list(
            part='snippet',
            type='video',
            eventType='live',
            maxResults=10
        ).execute()

        video_ids = [item['id']['videoId'] for item in search_today.get('items', []) if 'videoId' in item['id']]
        for item in live_search.get('items', []):
            vid = item['id'].get('videoId')
            if vid and vid not in video_ids:
                video_ids.append(vid)

        st.write(f"Debug: Today video IDs ({len(video_ids)}): {video_ids}")

        if not video_ids:
            return []

        # Query details & analytics similar to fetch_recent_videos_full but for these ids
        vids_resp = youtube.videos().list(part='snippet,statistics,status', id=','.join(video_ids)).execute()

        analytics_map = {}
        if analytics and channel_id_for_analytics:
            analytics_map = fetch_video_analytics_map(analytics, channel_id_for_analytics, video_ids, lookback_days=7)

        rows = []
        for v in vids_resp.get('items', []):
            vid = v['id']
            snippet = v.get('snippet', {})
            stats = v.get('statistics', {})
            status = v.get('status', {})

            view_count = int(stats.get('viewCount', 0))
            monetization = status.get('monetizationStatus') if status else None

            estimatedRevenue = analytics_map.get(vid, {}).get('estimatedRevenue') if analytics_map else None
            rpm = analytics_map.get(vid, {}).get('rpm') if analytics_map else None

            rows.append({
                'videoId': vid,
                'title': snippet.get('title'),
                'publishedAt': snippet.get('publishedAt'),
                'viewCount': view_count,
                'likeCount': int(stats.get('likeCount', 0)) if stats.get('likeCount') else None,
                'commentCount': int(stats.get('commentCount', 0)) if stats.get('commentCount') else None,
                'monetization': monetization,
                'estimatedRevenue': estimatedRevenue,
                'rpm': rpm
            })
        return rows

    except HttpError as e:
        st.error(f"YouTube API error: {e}")
        return []

# --------------------- CHANNEL & MONETIZATION ---------------------
def get_channel_stats(youtube, credentials=None, channel_id=None):
    try:
        if credentials:
            res = youtube.channels().list(part='snippet,statistics,status,contentDetails', mine=True).execute()
        else:
            if channel_id:
                res = youtube.channels().list(part='snippet,statistics,status,contentDetails', id=channel_id).execute()
            else:
                st.error('Please provide Channel ID in sidebar if not using OAuth.')
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

def get_channel_monetization_status(youtube):
    try:
        res = youtube.channels().list(part='status', mine=True).execute()
        items = res.get('items', [])
        if not items:
            return None
        return items[0].get('status', {})
    except HttpError as e:
        st.error(f"Error fetching channel monetization status: {e}")
        return None

# --------------------- ANALYTICS TIME-SERIES (unchanged) ---------------------
def fetch_analytics(analytics, channel_id, start_date, end_date):
    try:
        res = analytics.reports().query(
            ids='channel==' + channel_id,
            startDate=start_date,
            endDate=end_date,
            metrics='estimatedRevenue,views',
            dimensions='day'
        ).execute()
        rows = res.get('rows', []) or []
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

# --------------------- MAIN APP ---------------------
def main():
    credentials = None
    analytics = None

    # OAuth connect button
    if use_oauth:
        if os.path.exists(client_secrets_path):
            if st.sidebar.button('Connect via OAuth'):
                try:
                    creds = run_oauth_flow(client_secrets_path)
                    credentials = creds
                    st.success('OAuth connected — you can now fetch analytics & monetization.')
                    analytics = build('youtubeAnalytics', 'v2', credentials=creds)
                except Exception as e:
                    st.error(f"OAuth flow failed: {e}")
                    credentials = None
        else:
            st.sidebar.warning('client_secrets.json not found at provided path.')

    # Build YouTube client (either with OAuth creds or API key)
    yt = None
    if use_oauth and 'creds' in locals() and creds:
        yt = build_youtube(credentials=creds)
    elif api_key:
        yt = build_youtube(api_key=api_key)

    col1, col2 = st.columns([2, 1])

    # -------------- Realtime / Today Feeds --------------
    with col1:
        st.header('Realtime / Today Feeds')
        if yt:
            if st.button('Fetch videos published recently'):
                start_iso = default_published_after()

                # determine channel id for analytics (if OAuth)
                channel_id_for_analytics = None
                if analytics:
                    ch_info = get_channel_stats(yt, credentials=creds)
                    channel_id_for_analytics = ch_info['id'] if ch_info else None
                elif channel_id_input:
                    channel_id_for_analytics = channel_id_input

                videos = fetch_recent_videos_full(
                    yt,
                    published_after_iso=start_iso,
                    max_results=max_results,
                    analytics=analytics,
                    channel_id_for_analytics=channel_id_for_analytics
                )

                if not videos:
                    st.info('No videos found for recent period.')
                else:
                    df = pd.DataFrame(videos)
                    df['publishedAt'] = pd.to_datetime(df['publishedAt'])
                    df = df.sort_values('publishedAt')

                    st.subheader('Recent Videos (with Monetization & RPM if available)')
                    # show a nice table with the new columns
                    display_cols = ['title', 'videoId', 'publishedAt', 'viewCount', 'estimatedRevenue', 'rpm', 'monetization']
                    # guard columns existence
                    display_cols = [c for c in display_cols if c in df.columns]
                    st.dataframe(df[display_cols])

                    popular = df[df['viewCount'] >= 1000]
                    if not popular.empty:
                        st.subheader('Videos with ≥ 1000 views')
                        st.dataframe(popular[display_cols])
                    else:
                        st.info('No videos have reached 1000+ views yet.')
        else:
            st.info('API client not ready. Provide API key or OAuth.')

    # -------------- Channel Stats & Monetization --------------
    with col2:
        st.header('Channel / Analytics')
        if yt:
            if st.button('Get channel statistics'):
                ch = get_channel_stats(yt, credentials=creds if use_oauth and 'creds' in locals() else None,
                                       channel_id=channel_id_input if not use_oauth else None)
                if ch:
                    st.metric('Subscribers', f"{ch['subscribers']}")
                    st.metric('Channel Views', f"{ch['views']}")
                    st.metric('Total Videos', f"{ch['videos']}")
                    st.write('Channel status (API raw):')
                    st.json(ch['status'])

                    # show monetization status if OAuth
                    if use_oauth and 'creds' in locals() and creds:
                        monet = get_channel_monetization_status(yt)
                        if monet:
                            st.subheader('Monetization Status (channel)')
                            st.json(monet)
                else:
                    st.info('Unable to fetch channel stats. Provide channel ID if not using OAuth.')
        else:
            st.info('API client not ready. Provide API key or enable OAuth.')

    # --------------------- Analytics charts and RPM summary ---------------------
    st.header('YouTube Analytics (Estimated Revenue & Views)')
    with st.expander('Analytics chart controls (requires OAuth / analytics permission)'):
        channel_id_analytics = st.text_input('Channel ID for Analytics (leave empty to use authorized channel)', value='')
        start_date = st.date_input('Start date', value=(datetime.now() - timedelta(days=7)).date())
        end_date = st.date_input('End date', value=datetime.now().date())

        if analytics is None and use_oauth and 'creds' in locals() and creds:
            analytics = build('youtubeAnalytics', 'v2', credentials=creds)

        if st.button('Fetch analytics'):
            channel_id_for_analytics = None
            if channel_id_analytics:
                channel_id_for_analytics = channel_id_analytics
            else:
                # try to get authorized channel id
                if yt and use_oauth and 'creds' in locals() and creds:
                    chinfo = get_channel_stats(yt, credentials=creds)
                    channel_id_for_analytics = chinfo['id'] if chinfo else None
                elif channel_id_input:
                    channel_id_for_analytics = channel_id_input

            if not channel_id_for_analytics:
                st.error('Channel ID for analytics not available. Provide it or connect via OAuth.')
            elif analytics is None:
                st.error('Analytics client not available. Connect via OAuth and grant permissions.')
            else:
                df = fetch_analytics(analytics, channel_id_for_analytics, start_date.isoformat(), end_date.isoformat())
                if df.empty:
                    st.info('No analytics rows returned. Check permission, date range, or account access.')
                else:
                    df = compute_rpm(df)
                    st.dataframe(df)
                    avg_rpm = df['rpm'].mean()
                    st.metric(f"Average RPM ({start_date} to {end_date})", f"${avg_rpm:.2f}")

                    # Views chart
                    fig1 = plt.figure()
                    plt.plot(pd.to_datetime(df['date']), df['views'])
                    plt.title('Daily Views')
                    plt.xlabel('Date')
                    plt.ylabel('Views')
                    st.pyplot(fig1)

                    # Revenue chart
                    fig2 = plt.figure()
                    plt.plot(pd.to_datetime(df['date']), df['estimatedRevenue'])
                    plt.title('Estimated Revenue')
                    plt.xlabel('Date')
                    plt.ylabel('Revenue')
                    st.pyplot(fig2)

                    # RPM chart
                    fig3 = plt.figure()
                    plt.plot(pd.to_datetime(df['date']), df['rpm'])
                    plt.title('RPM (estimated)')
                    plt.xlabel('Date')
                    plt.ylabel('RPM')
                    st.pyplot(fig3)

    # Footer / notes
    with st.expander("Notes: RPM & Monetization"):
        st.markdown("""
        - RPM and Estimated Revenue are fetched from YouTube Analytics (requires OAuth & appropriate access).
        - Monetization status is returned by the Data API per-video under `status.monetizationStatus` (only for videos accessible to the authenticated account).
        - If you use only an API key (no OAuth), monetization and RPM columns will be empty because Analytics and `mine=true` require OAuth.
        - Analytics queries may return no data for very new videos or channels without revenue.
        - Quotas: be mindful of API quota consumption. Reduce polling frequency or max_results to save quota.
        """)

    st.success('App ready — use controls and connect OAuth for full RPM/Monetization data.')

if __name__ == "__main__":
    main()
