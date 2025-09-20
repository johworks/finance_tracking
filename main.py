# Allow a user to log which transactions they have made
# It will require a user to enter:
# type
# amount
# extra information

import pandas as pd
from sqlalchemy import create_engine, text
from datetime import datetime

def main():

    # Create or connect to an SQLite database
    engine = create_engine('sqlite:///transactions.db')
    table_name = 'transactions'

    with engine.connect() as conn:
        conn.execute(text(f'''
        CREATE TABLE IF NOT EXISTS {table_name} 
        (
        date TEXT DEFAULT CURRENT_TIMESTAMP,
        description TEXT,
        amount REAL, 
        category TEXT
        )
        '''))

    # User input loop
    while True:
        print("\nChoose an input:")
        print("1. Add transaction")
        print("2. Show transactions")
        print("3. Search transactions")
        print("4. Summary")
        print("5. Quit")
        choice = input("> ")

        if choice == "1":
            description = input("Description: ")
            amount = input("Amount: ")
            category = input("Category: ")
            add_transaction(engine, table_name, description, amount, category)
        elif choice == "2":
            show_transactions(engine, table_name)
        elif choice == "3":
            description = input("Description: ")
            amount = input("Amount: ")
            category = input("Category: ")
            rows, result = search_transactions(engine, table_name, description, amount, category)
            if rows:
                show_transactions(engine, table_name, rows, result)
        elif choice == "4":
            show_summary(engine, table_name)
        elif choice == "5":
            print("Goodbye :)")
            break
        else:
            print("Unknown command")

def show_summary(engine, table_name):

    # End goal: sum by total, category, date range
    # I want to first start by getting the total sum

    # I think I should somehow use the search_transactions
    # function to narrow down the values to print
    # To refactor, lower the scope of search_transactions
    # to return the SQL object instead of printing directly

    # Sum the entire amount coloumn
    query = f"SELECT sum(amount) FROM {table_name}"

    with engine.connect() as conn:
        result = conn.execute(text(query))
        rows = result.fetchall()

    if rows:
        show_transactions(engine, table_name, rows, result)

    # Sum by category
    query = f"SELECT category, SUM(amount) AS total_amount FROM {table_name} GROUP BY category"

    with engine.connect() as conn:
        result = conn.execute(text(query))
        rows = result.fetchall()

    if rows:
        show_transactions(engine, table_name, rows, result)

    # Narrow down to a date range
    query = f"SELECT category, SUM(amount) AS total_amount FROM {table_name}"
    query += " WHERE date >= DATE('now', '-3 days')  GROUP BY category"

    with engine.connect() as conn:
        result = conn.execute(text(query))
        rows = result.fetchall()

    if rows:
        show_transactions(engine, table_name, rows, result)

    #if rows:
    #    df = pd.DataFrame(rows, columns=result.keys())
    #    print("\nMatching transactions:")
    #    print(df)
    #else:
    #    print("No matching transactions found :(")


def search_transactions(engine, table_name, description=None, amount=None, category=None, match_any=True):
    filters = []
    params = {}
    if category:
        filters.append("category LIKE :category")
        params['category'] = f"%{category}%"
    if description:
        filters.append("description LIKE :description")
        params['description'] = f"%{description}%"
    if amount:
        filters.append("amount = :amount")
        params['amount'] = float(amount)


    query = f"SELECT * FROM {table_name}"
    if filters:
        connector = " OR " if match_any else " AND "
        query += " WHERE " + connector.join(filters)

    with engine.connect() as conn:
        result = conn.execute(text(query), params)
        rows = result.fetchall()

    return rows, result

    #if rows:
    #    df = pd.DataFrame(rows, columns=result.keys())
    #    print("\nMatching transactions:")
    #    print(df)
    #else:
    #    print("No matching transactions found :(")


def show_transactions(engine, table_name, rows=None, result=None):
    if not rows and not result:
        df = pd.read_sql(table_name, engine)
        print("\nAll Transactions:")
        print(df)
    elif result:
        df = pd.DataFrame(rows, columns=result.keys())
        print("\nMatching transactions:")
        print(df)
    else:
        print(f"The function show_transactions is not getting the proper inputs")

def add_transaction(engine, table_name, description, amount, category):
    date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    new_row = pd.DataFrame([{
        'date': date,
        'description': description,
        'amount': amount,
        'category': category
    }])
    new_row.to_sql(table_name, engine, if_exists='append', index=False)
    print(f"Added transaction: {description} (${amount}) on {date}")

if __name__ == "__main__":
    main()
