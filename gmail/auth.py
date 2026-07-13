import os
import json
from pathlib import Path
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request

import config


class GmailAuth:
    """Handles Gmail OAuth 2.0 authentication."""

    def __init__(self, credentials_file=None, token_file=None):
        self.credentials_file = Path(credentials_file or config.CREDENTIALS_FILE)
        self.token_file = Path(token_file or config.TOKEN_FILE)
        self.scopes = config.SCOPES
        self._credentials = None

    def has_credentials_file(self):
        """Check if credentials.json exists."""
        return self.credentials_file.exists()

    def is_authenticated(self):
        """Check if valid credentials exist."""
        creds = self.get_credentials()
        return creds is not None and creds.valid

    def get_credentials(self):
        """Get valid credentials, refreshing if needed."""
        if self._credentials and self._credentials.valid:
            return self._credentials

        creds = None

        # Load existing token
        if self.token_file.exists():
            try:
                creds = Credentials.from_authorized_user_file(
                    str(self.token_file), self.scopes
                )
            except Exception:
                creds = None

        # Refresh if expired
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_credentials(creds)
            except Exception:
                creds = None

        self._credentials = creds
        return creds

    def create_auth_flow(self, redirect_uri):
        """Create OAuth flow for web application."""
        if not self.has_credentials_file():
            raise FileNotFoundError(
                f"credentials.json not found at {self.credentials_file}. "
                "Please download it from Google Cloud Console."
            )

        flow = Flow.from_client_secrets_file(
            str(self.credentials_file),
            scopes=self.scopes,
            redirect_uri=redirect_uri
        )
        return flow

    def get_authorization_url(self, redirect_uri):
        """Get the URL to redirect user for OAuth consent."""
        flow = self.create_auth_flow(redirect_uri)
        auth_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent'
        )
        # PKCE: the code_verifier generated here must be reused at token exchange.
        return auth_url, state, getattr(flow, 'code_verifier', None)

    def complete_authentication(self, authorization_response, redirect_uri, code_verifier=None):
        """Complete OAuth flow with the authorization response."""
        flow = self.create_auth_flow(redirect_uri)
        if code_verifier:
            flow.code_verifier = code_verifier
        flow.fetch_token(authorization_response=authorization_response)

        creds = flow.credentials
        self._save_credentials(creds)
        self._credentials = creds

        return creds

    def _save_credentials(self, creds):
        """Save credentials to token file."""
        with open(self.token_file, 'w') as f:
            f.write(creds.to_json())

    def logout(self):
        """Remove stored credentials."""
        if self.token_file.exists():
            os.remove(self.token_file)
        self._credentials = None

    def get_user_email(self):
        """Get the authenticated user's email address."""
        creds = self.get_credentials()
        if not creds:
            return None

        # The email is stored in the token info
        if hasattr(creds, 'id_token') and creds.id_token:
            return creds.id_token.get('email')

        return None
