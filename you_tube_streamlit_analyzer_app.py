import streamlit as st
from datetime import datetime, timedelta, timezone
import pandas as pd
import matplotlib.pyplot as plt
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
import os
import json
import html

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

# Mock defaults when Analytics / OAuth not available
MOCK_RPM = 2.50
MOCK_MONETIZATION = "Enabled"

# --------------------- HELPERS / BUILDERS ---------------------
def default_published_after():
    # 3 days ago (UTC) default window
    return (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

@st.cache_resource
def build_youtube(api_key: str = None, credentials=None):
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
    except Exception as e:
        st.warning(f"Analytics fetching error: {e}")
        return {}

# --------------------- FETCH VIDEOS + MONETIZATION + RPM ---------------------
def enrich_videos_with_stats(youtube, video_ids, analytics_map=None, use_mock_if_missing=True):
    """
    Given video_ids, call videos().list to get snippet/statistics/status
    and return list of enriched dicts with monetization, rpm (from analytics_map or mock).
    """
    if not video_ids:
        return []

    try:
        vids_resp = youtube.videos().list(part='snippet,statistics,status', id=','.join(video_ids)).execute()
    except HttpError as e:
        st.error(f"YouTube videos.list error: {e}")
        return []
    except Exception as e:
        st.error(f"Unexpected error calling videos.list: {e}")
        return []

    rows = []
    for v in vids_resp.get('items', []):
        vid = v.get('id')
        snippet = v.get('snippet', {})
        stats = v.get('statistics', {})
        status = v.get('status', {})

        title = snippet.get('title', '')
        publishedAt = snippet.get('publishedAt', '')
        channel_title = snippet.get('channelTitle', '')
        channel_id = snippet.get('channelId', '')
        view_count = int(stats.get('viewCount', 0))
        like_count = int(stats.get('likeCount', 0)) if stats.get('likeCount') else None
        comment_count = int(stats.get('commentCount', 0)) if stats.get('commentCount') else None

        monetization_status = status.get('monetizationStatus')
        if monetization_status is None and use_mock_if_missing:
            monetization_status = MOCK_MONETIZATION

        # Analytics-based rpm if available, else mock
        rpm = None
        estimatedRevenue = None
        if analytics_map and vid in analytics_map:
            estimatedRevenue = analytics_map[vid].get('estimatedRevenue', 0.0)
            rpm = analytics_map[vid].get('rpm', MOCK_RPM)
        else:
            rpm = MOCK_RPM if use_mock_if_missing else None

        # Estimated earnings from RPM: (views / 1000) * rpm
        est_earnings = round((view_count / 1000.0) * rpm, 2) if rpm is not None else None

        # Thumbnail url
        thumbnail_url = None
        thumbs = snippet.get('thumbnails', {})
        if 'high' in thumbs:
            thumbnail_url = thumbs['high'].get('url')
        elif 'default' in thumbs:
            thumbnail_url = thumbs['default'].get('url')

        rows.append({
            'videoId': vid,
            'title': title,
            'publishedAt': publishedAt,
            'channelTitle': channel_title,
            'channelId': channel_id,
            'viewCount': view_count,
            'likeCount': like_count,
            'commentCount': comment_count,
            'monetization': monetization_status,
            'estimatedRevenue': estimatedRevenue,
            'rpm': rpm,
            'estEarnings': est_earnings,
            'thumbnail': thumbnail_url
        })
    return rows

def fetch_recent_videos_full(youtube, published_after_iso, max_results=10, analytics=None, channel_id_for_analytics=None, use_mock=True):
    """
    Search globally for videos published after 'published_after_iso' then enrich with monetization / rpm
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
        # debug output (truncated)
        st.write("Debug: Raw search response (truncated):")
        try:
            st.json({k: search_response.get(k) for k in ("kind", "etag", "pageInfo", "items") if k in search_response})
        except Exception:
            st.write("Debug: (unable to render JSON)")

        video_ids = [item['id']['videoId'] for item in search_response.get('items', []) if 'videoId' in item['id']]
        st.write(f"Debug: Video IDs found ({len(video_ids)}): {video_ids}")

        if len(video_ids) == 0:
            return []

        analytics_map = {}
        if analytics and channel_id_for_analytics:
            analytics_map = fetch_video_analytics_map(analytics, channel_id_for_analytics, video_ids, lookback_days=7)

        rows = enrich_videos_with_stats(youtube, video_ids, analytics_map if analytics_map else None, use_mock_if_missing=use_mock)
        return rows

    except HttpError as e:
        if hasattr(e, 'resp') and e.resp.status == 403:
            st.error("YouTube API quota exceeded or access denied.")
        else:
            st.error(f"YouTube API error: {e}")
        return []
    except Exception as e:
        st.error(f"Unexpected error fetching recent videos: {e}")
        return []

def fetch_today_videos_full(youtube, analytics=None, channel_id_for_analytics=None, max_results_today=50, use_mock=True):
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

        analytics_map = {}
        if analytics and channel_id_for_analytics:
            analytics_map = fetch_video_analytics_map(analytics, channel_id_for_analytics, video_ids, lookback_days=7)

        rows = enrich_videos_with_stats(youtube, video_ids, analytics_map if analytics_map else None, use_mock_if_missing=use_mock)
        return rows

    except HttpError as e:
        st.error(f"YouTube API error: {e}")
        return []
    except Exception as e:
        st.error(f"Unexpected error fetching today's videos: {e}")
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
    except Exception as e:
        st.error(f"Unexpected error fetching channel info: {e}")
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
    except Exception as e:
        st.error(f"Unexpected error fetching monetization status: {e}")
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
    except Exception as e:
        st.error(f"Unexpected analytics error: {e}")
        return pd.DataFrame()

def compute_rpm(df):
    df = df.copy()
    df['rpm'] = df.apply(lambda r: (r['estimatedRevenue'] / r['views'] * 1000) if r['views'] > 0 else 0, axis=1)
    return df

# --------------------- UI HELPERS ---------------------
def render_videos_markdown_table(rows):
    """
    Build a markdown table with thumbnail images and clickable titles and channel links.
    Expects list of dicts with keys: thumbnail, title, videoId, channelTitle, channelId, publishedAt, viewCount, rpm, monetization, estEarnings
    """
    if not rows:
        return "No videos to show."

    md = []
    # header
    md.append("| Thumbnail | Title | Channel | Published | Views | RPM ($) | Est. Earnings ($) | Monetization |")
    md.append("|---:|:---|:---|:---|---:|---:|---:|:---|")

    for r in rows:
        thumb = r.get('thumbnail') or ''
        thumb_md = f"<img src='{html.escape(thumb)}' width='120' />" if thumb else ""
        title = html.escape(r.get('title') or '')
        vid = r.get('videoId')
        title_md = f"[{title}](https://www.youtube.com/watch?v={vid})" if vid else title
        channel_title = html.escape(r.get('channelTitle') or '')
        channel_id = r.get('channelId')
        channel_md = f"[{channel_title}](https://www.youtube.com/channel/{channel_id})" if channel_id else channel_title
        published = r.get('publishedAt') or ''
        try:
            # shorter published
            published_s = pd.to_datetime(published).strftime("%Y-%m-%d %H:%M")
        except Exception:
            published_s = published
        views = r.get('viewCount', 0)
        rpm = r.get('rpm')
        rpm_s = f"{rpm:.2f}" if isinstance(rpm, (int, float)) else (str(rpm) if rpm is not None else "")
        est = r.get('estEarnings')
        est_s = f"{est:.2f}" if isinstance(est, (int, float)) else (str(est) if est is not None else "")
        monet = r.get('monetization') or ""
        # compose row
        md.append(f"| {thumb_md} | {title_md} | {channel_md} | {published_s} | {views} | {rpm_s} | {est_s} | {monet} |")

    return "\n".join(md)

def plot_views_chart_from_rows(rows, title):
    if not rows:
        st.info("No data to plot.")
        return
    df = pd.DataFrame(rows)
    if 'publishedAt' not in df.columns or 'viewCount' not in df.columns:
        st.info("Insufficient data for chart.")
        return
    # convert and sort
    df['publishedAt_dt'] = pd.to_datetime(df['publishedAt'], errors='coerce')
    df = df.sort_values('publishedAt_dt')
    plt.figure(figsize=(8, 3.5))
    plt.plot(df['publishedAt_dt'], df['viewCount'], marker='o')
    plt.xticks(rotation=35, ha='right')
    plt.title(title)
    plt.xlabel('Published')
    plt.ylabel('Views')
    plt.tight_layout()
    st.pyplot(plt)

# --------------------- MAIN APP ---------------------
def main():
    credentials = None
    analytics = None
    youtube_client = None

    # OAuth connect button
    if use_oauth:
        if os.path.exists(client_secrets_path):
            if st.sidebar.button('Connect via OAuth'):
                try:
                    credentials = run_oauth_flow(client_secrets_path)
                    st.success('OAuth connected — you can now fetch analytics & monetization.')
                    # build analytics client now
                    analytics = build('youtubeAnalytics', 'v2', credentials=credentials)
                except Exception as e:
                    st.error(f"OAuth flow failed: {e}")
                    credentials = None
        else:
            st.sidebar.warning('client_secrets.json not found at provided path.')

    # Build YouTube client (either with OAuth creds or API key)
    if credentials:
        youtube_client = build_youtube(credentials=credentials)
    elif api_key:
        youtube_client = build_youtube(api_key=api_key)
    else:
        youtube_client = None

    col1, col2 = st.columns([2, 1])

    # -------------- Realtime / Today Feeds --------------
    with col1:
        st.header('Realtime / Today Feeds')
        if youtube_client:
            if st.button('Fetch videos published recently'):
                start_iso = default_published_after()

                # determine channel id for analytics (if OAuth) or from sidebar
                channel_id_for_analytics = None
                if analytics and credentials:
                    ch_info = get_channel_stats(youtube_client, credentials=credentials)
                    channel_id_for_analytics = ch_info['id'] if ch_info else None
                elif channel_id_input:
                    channel_id_for_analytics = channel_id_input

                videos = fetch_recent_videos_full(
                    youtube_client,
                    published_after_iso=start_iso,
                    max_results=max_results,
                    analytics=analytics,
                    channel_id_for_analytics=channel_id_for_analytics,
                    use_mock=True
                )

                if not videos:
                    st.info('No videos found for recent period.')
                else:
                    # ensure datetime
                    for v in videos:
                        if 'publishedAt' in v:
                            try:
                                v['publishedAt'] = pd.to_datetime(v['publishedAt'])
                            except Exception:
                                pass

                    st.subheader('Recent Videos (with Monetization & RPM if available)')
                    md = render_videos_markdown_table(videos)
                    st.markdown(md, unsafe_allow_html=True)

                    # popular
                    popular = [v for v in videos if v.get('viewCount', 0) >= 1000]
                    if popular:
                        st.subheader('Videos with ≥ 1000 views')
                        st.markdown(render_videos_markdown_table(popular), unsafe_allow_html=True)
                    else:
                        st.info('No videos have reached 1000+ views yet.')

                    # chart
                    st.subheader('Views over time (Recent Videos)')
                    plot_views_chart_from_rows(videos, "Recent Videos — Views")
        else:
            st.info('API client not ready. Provide API key or OAuth.')

    # -------------- Channel Stats & Monetization --------------
    with col2:
        st.header('Channel / Analytics')
        if youtube_client:
            if st.button('Get channel statistics'):
                ch = get_channel_stats(youtube_client, credentials=credentials if credentials else None,
                                       channel_id=channel_id_input if not credentials else None)
                if ch:
                    st.metric('Subscribers', f"{ch['subscribers']}")
                    st.metric('Channel Views', f"{ch['views']}")
                    st.metric('Total Videos', f"{ch['videos']}")
                    st.write('Channel status (API raw):')
                    st.json(ch['status'] if 'status' in ch else {})

                    # show monetization status if OAuth
                    if credentials:
                        monet = get_channel_monetization_status(youtube_client)
                        if monet:
                            st.subheader('Monetization Status (channel)')
                            st.json(monet)
                else:
                    st.info('Unable to fetch channel stats. Provide channel ID if not using OAuth.')
        else:
            st.info('API client not ready. Provide API key or enable OAuth.')

    # --------------------- Today / Realtime section (separate button) ---------------------
    st.header('Today / Realtime (uploaded since UTC midnight + live)')
    if youtube_client:
        if st.button('Fetch today & live videos'):
            # decide analytics channel id
            channel_id_for_analytics = None
            if analytics and credentials:
                ch_info = get_channel_stats(youtube_client, credentials=credentials)
                channel_id_for_analytics = ch_info['id'] if ch_info else None
            elif channel_id_input:
                channel_id_for_analytics = channel_id_input

            today_videos = fetch_today_videos_full(
                youtube_client,
                analytics=analytics,
                channel_id_for_analytics=channel_id_for_analytics,
                max_results_today=max_results,
                use_mock=True
            )

            if not today_videos:
                st.info("No videos or livestreams found for today.")
            else:
                st.subheader("Today / Live Videos (with Monetization & RPM if available)")
                st.markdown(render_videos_markdown_table(today_videos), unsafe_allow_html=True)

                st.subheader("Views over time (Today / Live)")
                plot_views_chart_from_rows(today_videos, "Today / Live — Views")
    else:
        st.info('API client not ready. Provide API key or OAuth.')

    # --------------------- Analytics charts and RPM summary ---------------------
    st.header('YouTube Analytics (Estimated Revenue & Views)')
    with st.expander('Analytics chart controls (requires OAuth / analytics permission)'):
        channel_id_analytics = st.text_input('Channel ID for Analytics (leave empty to use authorized channel)', value='')
        start_date = st.date_input('Start date', value=(datetime.now() - timedelta(days=7)).date())
        end_date = st.date_input('End date', value=datetime.now().date())

        if analytics is None and credentials:
            try:
                analytics = build('youtubeAnalytics', 'v2', credentials=credentials)
            except Exception as e:
                st.error(f"Failed to build analytics client: {e}")
                analytics = None

        if st.button('Fetch analytics'):
            channel_id_for_analytics = None
            if channel_id_analytics:
                channel_id_for_analytics = channel_id_analytics
            else:
                # try to get authorized channel id
                if youtube_client and credentials:
                    chinfo = get_channel_stats(youtube_client, credentials=credentials)
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
        - If you use only an API key (no OAuth), the app uses mock values: `RPM = $2.50` and `Monetization = "Enabled"`.
        - Analytics queries may return no data for very new videos or channels without revenue.
        - Quotas: be mindful of API quota consumption. Reduce polling frequency or max_results to save quota.
        """)

    st.success('App ready — use controls and connect OAuth for full RPM/Monetization data.')

if __name__ == "__main__":
    main()
