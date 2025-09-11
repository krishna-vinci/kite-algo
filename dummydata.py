


import psycopg2
import random
from datetime import datetime, timedelta

# Update these connection details as needed.
DB_HOST = 'db'
DB_NAME = 'finance'
DB_USER = 'krishna'
DB_PASS = '1122'

def insert_dummy_data():
    conn = psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS
    )
    cur = conn.cursor()
    
    tickers = ['Nifty50', 'Bank Nifty', 'Gold']
    strategies = ['Bull Spread', 'Iron Condor', 'Long Call', 'Short Put']
    base_date = datetime(2023, 1, 1)
    
    for _ in range(100):
        trade_date = base_date + timedelta(days=random.randint(0, 364))
        ticker = random.choice(tickers)
        strategy = random.choice(strategies)
        trade_result = 'W' if random.random() > 0.5 else 'L'
        p_and_l = round(random.uniform(-1000, 1000), 2)
        margin = round(random.uniform(1000, 10000), 2)
        created_at = datetime.now() - timedelta(days=random.randint(0, 100))
        updated_at = datetime.now()
        
        insert_query = """
        INSERT INTO trade_journal (trade_date, ticker, strategy, trade_result, p_and_l, margin, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
        """
        cur.execute(insert_query, (trade_date, ticker, strategy, trade_result, p_and_l, margin, created_at, updated_at))
        
    conn.commit()
    cur.close()
    conn.close()

if __name__ == "__main__":
    insert_dummy_data()
    print("Dummy data inserted successfully.")
