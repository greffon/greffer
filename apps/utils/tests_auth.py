import unittest
from unittest.mock import patch, MagicMock


class GetTokenTests(unittest.TestCase):
    """Tests for get_token."""

    def test_get_token_generates_once(self):
        """Calling get_token twice should return the same value."""
        import apps.utils.auth as auth_module

        # Reset the module-level token to None to ensure fresh state
        auth_module.token = None

        token1 = auth_module.get_token()
        token2 = auth_module.get_token()

        self.assertIsNotNone(token1)
        self.assertEqual(token1, token2)

        # Clean up: reset token so other tests are not affected
        auth_module.token = None


class IsLoggedTests(unittest.TestCase):
    """Tests for the is_logged decorator."""

    def setUp(self):
        import apps.utils.auth as auth_module
        # Set a known token for testing
        auth_module.token = 'test-token-abc'
        self.auth_module = auth_module

    def tearDown(self):
        # Reset token after each test
        self.auth_module.token = None

    def test_is_logged_valid_token(self):
        """Request with matching X-GREFFON-TOKEN header should call the
        wrapped function and return its result."""
        from apps.utils.auth import is_logged

        @is_logged
        def my_view(request):
            return 'success'

        mock_request = MagicMock()
        mock_request.headers = {'X-GREFFON-TOKEN': 'test-token-abc'}

        result = my_view(mock_request)
        self.assertEqual(result, 'success')

    def test_is_logged_invalid_token(self):
        """Request with wrong token should return 401."""
        from apps.utils.auth import is_logged

        @is_logged
        def my_view(request):
            return 'success'

        mock_request = MagicMock()
        mock_request.headers = {'X-GREFFON-TOKEN': 'wrong-token'}

        result = my_view(mock_request)
        self.assertEqual(result.status_code, 401)

    def test_is_logged_missing_token(self):
        """Request with no X-GREFFON-TOKEN header should return 401."""
        from apps.utils.auth import is_logged

        @is_logged
        def my_view(request):
            return 'success'

        mock_request = MagicMock()
        mock_request.headers = {}

        result = my_view(mock_request)
        self.assertEqual(result.status_code, 401)
