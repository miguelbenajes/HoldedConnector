import os
import requests
from dotenv import load_dotenv
import time

load_dotenv()
API_KEY = os.getenv("HOLDED_API_KEY")
BASE_URL = "https://api.holded.com/api"
HEADERS = {
    "key": API_KEY,
    "Content-Type": "application/json"
}

def test_fetch(endpoint, params=None):
    url = f"{BASE_URL}{endpoint}"
    print(f"Fetching {url} with params {params}")
    response = requests.get(url, headers=HEADERS, params=params)
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"Count: {len(data)}")
        if len(data) > 0:
            print(f"First item date: {data[0].get('date')}")
            print(f"Last item date: {data[-1].get('date')}")
        return data
    else:
        print(f"Error: {response.text}")
        return None

if __name__ == "__main__":
    params = {
        "starttmp": 1262304000,
        "endtmp": int(time.time())
    }
    data = test_fetch("/invoicing/v1/documents/invoice", params=params)
    if data and len(data) > 0:
        first_invoice = data[0]
        print("\n--- First Invoice Keys ---")
        print(first_invoice.keys())
        if 'products' in first_invoice:
            print("\n--- Products (Line Items) in First Invoice ---")
            products = first_invoice['products']
            print(f"Total line items: {len(products)}")
            if len(products) > 0:
                print("First line item keys:", products[0].keys())
                print("First line item content:", products[0])
        else:
            print("\nNo 'products' key found in invoice.")
