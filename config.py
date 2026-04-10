import os
from pathlib import Path

# Base directory
BASE_DIR = Path(__file__).resolve().parent

# Flask settings
SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
DEBUG = os.environ.get('DEBUG', 'True').lower() == 'true'

# Google OAuth settings
CREDENTIALS_FILE = BASE_DIR / 'credentials.json'
TOKEN_FILE = BASE_DIR / 'token.json'

# Gmail API scopes
# - gmail.readonly: Read email metadata and content
# - gmail.modify: Modify labels and move to trash
# - gmail.settings.basic: Read settings (for labels)
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
]

# API settings
MAX_RESULTS_PER_PAGE = 500  # Max emails to fetch per API call
MAX_EMAILS_TO_ANALYZE = None  # No limit - analyze all emails
BATCH_DELETE_SIZE = 50  # Emails to delete per batch

# Pattern detection settings
MIN_PATTERN_COUNT = 5  # Minimum emails to form a pattern
AGE_GROUPS_DAYS = [30, 90, 180, 365, 730]  # Age buckets in days
LARGE_EMAIL_SIZE_KB = 1024  # Consider emails > 1MB as large

# Gmail category labels
CATEGORY_LABELS = [
    'CATEGORY_PROMOTIONS',
    'CATEGORY_SOCIAL',
    'CATEGORY_UPDATES',
    'CATEGORY_FORUMS',
]

# Rules settings
RULES_FILE = BASE_DIR / 'rules.json'
EMAIL_INDEX_FILE = BASE_DIR / 'email_index.json'
MAX_RULE_PREVIEW = 10000

# Unsubscribe settings
UNSUBSCRIBE_TRACKING_FILE = BASE_DIR / 'unsubscribe_tracking.json'
USER_ACTIONS_FILE = BASE_DIR / 'user_actions.json'
UNSUBSCRIBE_HTTP_TIMEOUT = 15  # seconds
UNSUBSCRIBE_BATCH_DELAY = 2  # seconds between requests
UNSUBSCRIBE_SUGGESTION_MIN_EMAILS = 10
UNSUBSCRIBE_SUGGESTION_MIN_UNREAD_RATIO = 0.7

# Optional scope for sending unsubscribe emails via mailto
SCOPES_WITH_SEND = SCOPES + [
    'https://www.googleapis.com/auth/gmail.send',
]
