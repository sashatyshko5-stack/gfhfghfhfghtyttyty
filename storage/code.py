from pyrogram import Client

API_ID = 38876656         # сюда свой api_id
API_HASH = "99ce1cc52f2c70fa7478ef3e27e90662"  # сюда свой api_hash

with Client(
    "session",
    api_id=API_ID,
    api_hash=API_HASH,
    in_memory=True
) as app:
    print("\nSession String:\n")
    print(app.export_session_string())