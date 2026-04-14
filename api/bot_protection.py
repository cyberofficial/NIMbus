"""Bot detection and IP banning system."""

from pathlib import Path

from fastapi import Request
from fastapi.responses import PlainTextResponse, Response
from loguru import logger


class BotProtection:
    """Manages bot detection and IP banning."""

    def __init__(self, ban_list_path: str = "banned_ips.txt"):
        self.ban_list_path = Path(ban_list_path)
        self.failed_attempts: dict[str, int] = {}
        self._banned_ips: set[str] = set()
        self._load_banned_ips()

    def _load_banned_ips(self) -> None:
        """Load banned IPs from file."""
        if self.ban_list_path.exists():
            try:
                with open(self.ban_list_path) as f:
                    self._banned_ips = {line.strip() for line in f if line.strip()}
                logger.info(
                    f"Loaded {len(self._banned_ips)} banned IPs from {self.ban_list_path}"
                )
            except Exception as e:
                logger.error(f"Failed to load banned IPs: {e}")
                self._banned_ips = set()
        else:
            self._banned_ips = set()
            logger.info("No existing ban list found, starting fresh")

    def _save_banned_ips(self) -> None:
        """Save banned IPs to file."""
        try:
            with open(self.ban_list_path, "w") as f:
                for ip in self._banned_ips:
                    f.write(f"{ip}\n")
            logger.info(
                f"Saved {len(self._banned_ips)} banned IPs to {self.ban_list_path}"
            )
        except Exception as e:
            logger.error(f"Failed to save banned IPs: {e}")

    def is_banned(self, ip: str) -> bool:
        """Check if an IP is banned."""
        return ip in self._banned_ips

    def ban_ip(self, ip: str) -> None:
        """Ban an IP address."""
        if ip not in self._banned_ips:
            self._banned_ips.add(ip)
            self._save_banned_ips()
            logger.warning(f"BANNED IP: {ip}")

    def record_failed_attempt(self, ip: str) -> Response | None:
        """
        Record a failed attempt for an IP.
        If threshold exceeded, ban the IP and return ban response.
        Otherwise, return None.
        """
        if ip in self._banned_ips:
            return self.get_ban_response()

        # Increment failed attempt counter
        self.failed_attempts[ip] = self.failed_attempts.get(ip, 0) + 1

        # Log the attempt
        attempts = self.failed_attempts[ip]
        logger.warning(f"Suspicious activity from {ip}: {attempts} failed attempts")

        # Check if threshold exceeded (2 failed attempts)
        if attempts >= 2:
            self.ban_ip(ip)
            # Clean up the counter for banned IP
            del self.failed_attempts[ip]
            return self.get_ban_response()

        return None

    def get_ban_response(self) -> Response:
        """Get the response to show to banned IPs."""
        return PlainTextResponse(
            "FUCK OFF YOU BOT BANNED",
            status_code=403,
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )

    def get_banned_ips(self) -> set[str]:
        """Get the set of banned IPs."""
        return self._banned_ips.copy()


# Global instance
_instance: BotProtection | None = None


def get_bot_protection() -> BotProtection:
    """Get the global BotProtection instance."""
    global _instance
    if _instance is None:
        _instance = BotProtection()
    return _instance


def is_valid_request_path(path: str) -> bool:
    """Check if a request path is valid (not a bot scan attempt)."""
    # Valid paths for the application
    valid_paths = {
        "/",
        "/health",
        "/status",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/v1/messages",
        "/v1/messages/count_tokens",
        "/stop",
        "/favicon.ico",
    }

    # Check exact match
    if path in valid_paths:
        return True

    # Check if path starts with valid prefixes
    if (
        path.startswith("/docs")
        or path.startswith("/redoc")
        or path.startswith("/admin")
    ):
        return True

    return False


def is_suspicious_path(path: str) -> bool:
    """Check if path looks like a fuzzy search/suggestion bot scan."""
    # Suspicious patterns often used by bots scanning for suggestion APIs
    suspicious_patterns = [
        "/suggest",
        "/suggestion",
        "/search",
        "/api/suggest",
        "/api/suggestions",
        "/api/search",
        "/autocomplete",
        "/auto-complete",
        "/fuzzy",
        "/query",
        "/api/query",
    ]

    path_lower = path.lower()
    for pattern in suspicious_patterns:
        if pattern in path_lower:
            return True
    return False


def is_instant_ban_path(path: str) -> bool:
    """Check if path should trigger instant ban."""
    # Paths that trigger immediate ban (1st attempt = ban)
    instant_ban_paths = {
        "/robots.txt",
        "/sitemap.xml",
        "/sitemap_index.xml",
    }

    # Check exact match
    if path in instant_ban_paths:
        return True

    # Check for path traversal attempts
    if "../" in path or "%5c" in path.lower() or "%2e" in path.lower():
        return True

    return False


async def check_bot_protection(request: Request) -> Response | None:
    """
    Check if request is from a banned IP or suspicious activity.
    Returns ban response if banned, None otherwise.

    Note: Valid API key users bypass this check entirely.
    """
    # Get client IP
    if not request.client:
        return None

    client_ip = request.client.host
    bot_protection = get_bot_protection()

    # Check if already banned - ban applies to ALL paths
    if bot_protection.is_banned(client_ip):
        logger.warning(f"Blocked banned IP: {client_ip} attempting {request.url.path}")
        return bot_protection.get_ban_response()

    # Check for instant ban paths (robots.txt, sitemap.xml, path traversal)
    if is_instant_ban_path(request.url.path):
        logger.warning(
            f"INSTANT BAN triggered: {client_ip} accessed {request.url.path}"
        )
        bot_protection.ban_ip(client_ip)
        return bot_protection.get_ban_response()

    # Check if request path is valid
    if not is_valid_request_path(request.url.path):
        logger.info(f"Invalid path detected from {client_ip}: {request.url.path}")
        return bot_protection.record_failed_attempt(client_ip)

    # Check for fuzzy search/suggestion patterns (immediate ban on suspicious paths)
    if is_suspicious_path(request.url.path):
        logger.warning(
            f"Suspicious fuzzy search path detected from {client_ip}: {request.url.path}"
        )
        return bot_protection.record_failed_attempt(client_ip)

    # Check for suspicious User-Agent patterns (optional additional check)
    user_agent = request.headers.get("User-Agent", "").lower()
    suspicious_patterns = ["bot", "crawler", "spider", "scraper", "scan"]
    if any(pattern in user_agent for pattern in suspicious_patterns):
        logger.warning(
            f"Suspicious User-Agent from {client_ip}: {request.headers.get('User-Agent')}"
        )
        # Don't auto-ban on User-Agent alone, just log it

    return None
