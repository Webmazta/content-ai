import requests
import base64
import os
from dotenv import load_dotenv

load_dotenv()

WP_URL = os.getenv("WP_URL")
WP_USERNAME = os.getenv("WP_USERNAME")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")

def _wp_auth():
    return (WP_USERNAME, WP_APP_PASSWORD)

def audit_post(post_id):
    print(f"Auditing Post ID: {post_id}")
    url = f"{WP_URL}/wp-json/wp/v2/posts/{post_id}"
    
    try:
        response = requests.get(url, auth=_wp_auth())
        if response.status_code == 200:
            data = response.json()
            print(f"Title: {data['title']['rendered']}")
            print(f"Featured Media ID: {data['featured_media']}")
            print("--- Content Begin ---")
            print(data['content']['rendered'])
            print("--- Content End ---")
            
            # Check featured media
            if data['featured_media'] > 0:
                media_url = f"{WP_URL}/wp-json/wp/v2/media/{data['featured_media']}"
                m_resp = requests.get(media_url, auth=_wp_auth())
                if m_resp.status_code == 200:
                    m_data = m_resp.json()
                    print(f"Featured Image URL: {m_data['source_url']}")
                else:
                    print(f"Failed to fetch featured media info: {m_resp.status_code}")
        else:
            print(f"Failed to fetch post: {response.status_code}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        post_id = int(sys.argv[1])
    else:
        # Fallback to the last results.json id if possible, 
        # but here we'll just require an arg or use a default.
        post_id = 1058
    audit_post(post_id)
