import argparse
import connector
import sqlite3
import time

def list_summary():
    conn = sqlite3.connect(connector.DB_NAME)
    cursor = conn.cursor()
    
    entities = {
        "Invoices": "invoices",
        "Purchases": "purchase_invoices",
        "Estimates": "estimates",
        "Products": "products",
        "Contacts": "contacts",
        "Projects": "projects",
        "Payments": "payments"
    }
    
    print("\n--- Local Database Summary ---")
    for name, table in entities.items():
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        count = cursor.fetchone()[0]
        print(f"{name:12}: {count} items")
    conn.close()

def main():
    parser = argparse.ArgumentParser(description="Holded Command Center")
    parser.add_argument("--sync", action="store_true", help="Synchronize all data from Holded")
    parser.add_argument("--summary", action="store_true", help="Show local database summary")
    parser.add_argument("--create-test-contact", action="store_true", help="Create a test contact in Holded")
    
    args = parser.parse_args()
    
    if args.sync:
        print("Starting full synchronization...")
        connector.init_db()
        connector.sync_contacts()
        connector.sync_products()
        connector.sync_invoices()
        connector.sync_purchases()
        connector.sync_estimates()
        connector.sync_projects()
        connector.sync_payments()
        print("\nSync completed!")
        list_summary()
        
    elif args.summary:
        list_summary()
        
    elif args.create_test_contact:
        contact_data = {
            "name": "Test Client " + str(int(time.time())),
            "email": "test@example.com",
            "type": "client"
        }
        contact_id = connector.create_contact(contact_data)
        if contact_id:
            print(f"Success! Created contact ID: {contact_id}")
            print("Run with --sync to see it in your local database.")
    
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
