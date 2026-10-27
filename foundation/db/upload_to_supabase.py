import os
import pandas as pd
from supabase import create_client, Client

# Replace with your Supabase Project URL and Service Role Key (from Project Settings -> API)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://YOUR_PROJECT_ID.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "YOUR_SERVICE_ROLE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def upload_in_batches(table_name: str, csv_path: str, batch_size: int = 1000):
    print(f"Loading {csv_path} into {table_name}...")
    df = pd.read_csv(csv_path)
    records = df.to_dict(orient="records")
    total = len(records)
    for i in range(0, total, batch_size):
        chunk = records[i:i + batch_size]
        supabase.table(table_name).insert(chunk).execute()
        print(f"  Uploaded {min(i + batch_size, total):,}/{total:,} rows to {table_name}")
    print(f"Finished {table_name}!\n")

if __name__ == "__main__":
    upload_in_batches("branches", "supabase_01_branches.csv")
    upload_in_batches("suppliers", "supabase_02_suppliers.csv")
    upload_in_batches("products", "supabase_03_products_all.csv")
    upload_in_batches("product_batches", "supabase_04_product_batches.csv")
    upload_in_batches("sales_transactions", "sales_transactions_Maadi.csv", batch_size=2000)
    upload_in_batches("sales_transactions", "sales_transactions_Nasr_City.csv", batch_size=2000)
    upload_in_batches("sales_transactions", "sales_transactions_6th_October.csv", batch_size=2000)
