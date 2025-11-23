import sqlite3

# Connect to database
conn = sqlite3.connect('energy_monitor.db')

print("=" * 60)
print("DATABASE VERIFICATION")
print("=" * 60)

# Check all tables
print("\n📋 Tables in database:")
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
for table in tables:
    print(f"  ✓ {table[0]}")

# Check schedules table structure
print("\n📊 Schedules table structure:")
try:
    columns = conn.execute("PRAGMA table_info(schedules)").fetchall()
    for col in columns:
        print(f"  - {col[1]} ({col[2]})")
except Exception as e:
    print(f"  ❌ ERROR: {e}")

# Check if there are any schedules
print("\n📅 Existing schedules:")
try:
    schedules = conn.execute("SELECT * FROM schedules").fetchall()
    if schedules:
        for s in schedules:
            print(f"  ID {s[0]}: {s[2]} | {s[3]}-{s[4]} | Days: {s[5]} | Enabled: {s[6]}")
    else:
        print("  (No schedules created yet)")
except Exception as e:
    print(f"  ❌ ERROR: {e}")

# Check devices
print("\n🔌 Devices:")
try:
    devices = conn.execute("SELECT device_id, device_name FROM devices").fetchall()
    for d in devices:
        print(f"  - {d[1]} ({d[0]})")
except Exception as e:
    print(f"  ❌ ERROR: {e}")

conn.close()
print("\n" + "=" * 60)
print("✅ Verification complete!")
print("=" * 60)