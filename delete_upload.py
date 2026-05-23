import sqlite3

conn = sqlite3.connect("chat_history.db")

cursor = conn.cursor()

cursor.execute("""
DELETE FROM uploaded_files
""")

conn.commit()

conn.close()