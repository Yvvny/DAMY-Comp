from __future__ import annotations

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.send',
]

WKHTMLTOPDF_PATH = r'C:\Users\amcny\Michael Python\Order Import\wkhtmltopdf\bin\wkhtmltopdf.exe'

ORDER_SOURCES = {
    'godaddy': {
        'from_address': 'noreply@mysimplestore.com',
        'base_directory': r'T:\DAMY',
        'display_name': 'GoDaddy',
        'use_broad_search_first': False,
        'gmail_label': None,
    },
    'photodeck': {
        'from_address': 'noreply-sites+2d01e0e6-82db-4957-855e-722dd89bf25c@photodeck.email',
        'base_directory': r'T:\DAMY PROOF',
        'display_name': 'PhotoDeck',
        'use_broad_search_first': True,
        'gmail_label': 'PHOTODECK PAID ORDER',
        'gmail_imported_label': 'PHOTODECK PAID ORDER IMPORTED',
    },
}

MAX_RESULTS_PER_QUERY = 100
SEARCH_WINDOW_DAYS = 90
MAX_MATCHES_PER_ID = 30
