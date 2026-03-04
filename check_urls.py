import requests
import os

urls = [
    "https://afripulsetv.com/wp-content/uploads/2026/02/flyer-1770957925.jpg",
    "https://afripulsetv.com/wp-content/uploads/2026/02/featured-1770957925.jpg"
]

def check_urls():
    for url in urls:
        print(f"Checking URL: {url}")
        try:
            resp = requests.head(url, timeout=10)
            print(f"  Status: {resp.status_code}")
            print(f"  Content-Type: {resp.headers.get('Content-Type')}")
            print(f"  Content-Length: {resp.headers.get('Content-Length')}")
            
            if resp.status_code != 200:
                # Try GET if HEAD is blocked
                resp = requests.get(url, timeout=10, stream=True)
                print(f"  GET Status: {resp.status_code}")
                if resp.status_code == 200:
                    print(f"  GET Content-Type: {resp.headers.get('Content-Type')}")
        except Exception as e:
            print(f"  Error: {e}")

if __name__ == "__main__":
    check_urls()
