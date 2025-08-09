# YouTube Realtime & Daily Analyzer - Streamlit App

This Streamlit app allows you to:

- Fetch videos published *today* on YouTube and show those that reached **1000+ views** (approximate fastest to 1k views).
- List all videos published on the current day.
- Check channel statistics such as subscribers, total views, and total videos.
- Fetch YouTube Analytics data (estimated revenue and views) for a chosen date range and calculate RPM.
- Display line charts for views, estimated revenue, and RPM over time.

## Features

- Uses YouTube Data API v3 for video and channel data.
- Uses YouTube Analytics API v2 for revenue and advanced stats (OAuth required).
- Supports OAuth 2.0 authentication or API key access (for public data).
- Handles quota limitations by limiting max results and polling intervals.
- Visualizes data with Matplotlib and Pandas in Streamlit.

## Setup

### Prerequisites

1. Python 3.7+  
2. Google Cloud project with:
   - YouTube Data API enabled
   - YouTube Analytics API enabled (for RPM/monetization data)
3. OAuth 2.0 credentials (optional but recommended for Analytics)  
4. YouTube Data API key (for basic public data access)

### Installation

```bash
pip install -r requirements.txt
