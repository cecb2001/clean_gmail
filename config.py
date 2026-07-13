import os
import re
from pathlib import Path

# Base directory
BASE_DIR = Path(__file__).resolve().parent

# Flask settings
SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
DEBUG = os.environ.get('DEBUG', 'True').lower() == 'true'

# Google OAuth settings
CREDENTIALS_FILE = BASE_DIR / 'credentials.json'
TOKEN_FILE = BASE_DIR / 'token.json'
PREFERENCES_FILE = BASE_DIR / 'preferences.json'

# Gmail API scopes
# - gmail.readonly: Read email metadata and content
# - gmail.modify: Modify labels and move to trash
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
]

# API settings
MAX_RESULTS_PER_PAGE = 500
MAX_EMAILS_TO_ANALYZE = None
BATCH_DELETE_SIZE = 50

# Pattern detection settings
MIN_PATTERN_COUNT = 5
AGE_GROUPS_DAYS = [30, 90, 180, 365, 730]
LARGE_EMAIL_SIZE_KB = 1024

# Gmail category labels
CATEGORY_LABELS = [
    'CATEGORY_PROMOTIONS',
    'CATEGORY_SOCIAL',
    'CATEGORY_UPDATES',
    'CATEGORY_FORUMS',
]

# --- Intelligent cleanup: scoring signal weights ---
# Kept in one place so tests and the runtime read the same numbers.
PROTECT_WEIGHTS = {
    'allowlisted':          200,
    'you_replied':          150,
    'high_read_rate':       120,
    'starred':              100,
    'transactional':         80,
    'important_label':       50,
    'medium_read_rate':      50,
}

JUNK_WEIGHTS = {
    'denylisted':           200,
    'zero_read_rate':        40,
    'unread_and_old':        30,
    'has_unsubscribe':       20,
    'promotions_category':   15,
}

# Read-rate tier thresholds
READ_RATE_MIN_SAMPLES = 5
READ_RATE_HIGH = 0.7
READ_RATE_MEDIUM_MIN = 0.3

# Zero-read-rate junk signal requires at least this many emails to fire.
ZERO_READ_MIN_SAMPLES = 5

# Age (days) threshold for the "unread and old" junk signal.
UNREAD_OLD_AGE_DAYS = 90

# --- Cleanup presets ---
# min_confidence: delete_confidence threshold to recommend.
# max_protect_score: exclude any email with a protect signal at or above this bar.
CLEANUP_PRESETS = {
    'conservative': {
        'min_confidence': 60,
        'max_protect_score': 40,
        'label': 'Conservative',
        'description': 'Recommend only when signals strongly agree. Excludes any email with a mild protect signal (IMPORTANT label, medium engagement, or stronger).',
    },
    'balanced': {
        'min_confidence': 40,
        'max_protect_score': 40,
        'label': 'Balanced',
        'description': 'Wider net for junk, still excludes anything with a moderate protect signal.',
    },
    'aggressive': {
        'min_confidence': 20,
        'max_protect_score': 100,
        'label': 'Aggressive',
        'description': 'Broadest cleanup. Only strongly protected emails (starred, transactional, replied-to, high read-rate, allowlisted) are excluded.',
    },
}

DEFAULT_PRESET = 'conservative'

# --- Transactional protection heuristics ---
TRANSACTIONAL_SUBJECT_REGEX = re.compile(
    r'\b('
    r'receipt|invoice|statement|order|confirmation|confirmed|'
    r'shipped|delivered|dispatch|dispatched|tracking|'
    r'boarding|itinerary|reservation|booking|'
    r'tax|refund|payment|payslip|payroll|'
    r'policy|renewal|premium|'
    r'subscription\s+(renewed|renewal|charged)|'
    r'your\s+(order|package|delivery)'
    r')\b',
    re.IGNORECASE,
)

# Domain seeds that mark a sender as transactional/critical regardless of subject.
# Match is "sender_domain equals one of these OR endswith '.' + one of these".
TRANSACTIONAL_DOMAIN_SEEDS = frozenset([
    # Banks / financial institutions
    'chase.com', 'wellsfargo.com', 'capitalone.com', 'bankofamerica.com',
    'citi.com', 'citibank.com', 'amex.com', 'americanexpress.com',
    'discover.com', 'usbank.com', 'schwab.com', 'fidelity.com', 'vanguard.com',
    'hdfcbank.com', 'sbi.co.in', 'icicibank.com', 'axisbank.com',
    'barclays.co.uk', 'lloydsbank.com', 'hsbc.com', 'natwest.com',
    'santander.com', 'deutsche-bank.de', 'ing.com', 'bnpparibas.com',
    # Payments
    'paypal.com', 'stripe.com', 'revolut.com', 'wise.com', 'transferwise.com',
    'venmo.com', 'cashapp.com', 'zellepay.com', 'squareup.com',
    # Shipping / logistics
    'ups.com', 'fedex.com', 'dhl.com', 'usps.com', 'royalmail.com',
    'amazon.com', 'amazon.co.uk', 'amazon.in',
    # Airlines / travel
    'united.com', 'delta.com', 'aa.com', 'americanair.com', 'southwest.com',
    'british-airways.com', 'ba.com', 'lufthansa.com', 'airfrance.fr',
    'emirates.com', 'qatarairways.com', 'singaporeair.com', 'airindia.in',
    'booking.com', 'airbnb.com', 'expedia.com', 'hotels.com',
    'uber.com', 'lyft.com',
    # Government / tax
    'irs.gov', 'ssa.gov', 'usa.gov',
    'hmrc.gov.uk', 'gov.uk',
    'incometax.gov.in',
    # Utilities / telecom
    'att.com', 'verizon.com', 'tmobile.com', 'sprint.com', 'xfinity.com',
    'comcast.com', 'spectrum.com', 'centurylink.com',
    'vodafone.com', 'o2.co.uk', 'ee.co.uk', 'threestore.co.uk',
    'jio.com', 'airtel.com',
    # Employers / payroll platforms
    'workday.com', 'adp.com', 'gusto.com', 'paychex.com', 'bamboohr.com',
    # Insurance
    'geico.com', 'progressive.com', 'statefarm.com', 'allstate.com',
    # Health
    'kaiserpermanente.org', 'aetna.com', 'anthem.com', 'cigna.com',
])

# SENT pre-scan settings
SENT_PRESCAN_MAX = 2000
SENT_CACHE_TTL_HOURS = 24

# Analysis persistence (SQLite)
ANALYSIS_DB_FILE = BASE_DIR / 'analysis.db'
ANALYSIS_TTL_DAYS = 7
