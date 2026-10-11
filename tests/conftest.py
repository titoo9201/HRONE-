import os

from dotenv import load_dotenv

load_dotenv()  # same precedence as the app: real env > .env > the test defaults below

# app.main refuses to import without these. Creating the client does not connect, so
# the unit tests need no running MongoDB. Real env / .env values still win.
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGO_DB", "attendance_test")
