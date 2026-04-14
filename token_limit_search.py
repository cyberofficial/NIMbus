#!/usr/bin/env python3
"""Extract NVIDIA NIM max token limit from API error response."""

import asyncio
import os
import re
import sys
from pathlib import Path

import httpx


# Load .env file
def load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value


load_env()


async def get_token_limit(api_key: str, model: str) -> int | None:
    """Extract max token limit from API error response."""
    url = "https://integrate.api.nvidia.com/v1/chat/completions"

    # Request way over limit to ensure error
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 1_000_000,
        "temperature": 0.0,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as client:
        max_retries = 5
        for attempt in range(max_retries):
            try:
                response = await client.post(
                    url, json=payload, headers=headers, timeout=60.0
                )

                if response.status_code == 200:
                    print("  1M tokens succeeded - searching for upper limit...")
                    # Binary search for upper limit
                    low, high = 1_000_000, 100_000_000
                    while high - low > 10_000:
                        mid = (low + high) // 2
                        test_payload = {
                            "model": model,
                            "messages": [{"role": "user", "content": "Hi"}],
                            "max_tokens": mid,
                            "temperature": 0.0,
                        }
                        test_resp = await client.post(
                            url, json=test_payload, headers=headers, timeout=60.0
                        )

                        if test_resp.status_code == 429:
                            print("    ⏳ Rate limited, waiting 62s...")
                            await asyncio.sleep(62)
                            continue

                        if test_resp.status_code == 200:
                            low = mid
                            print(f"    ✅ {mid:,} works")
                        else:
                            high = mid
                            # Try to parse limit from error
                            match = re.search(r"max_total_tokens=(\d+)", test_resp.text)
                            if match:
                                return int(match.group(1))
                            print(f"    ❌ {mid:,} fails")

                    # Verify the found limit
                    final_limit = low
                    print(f"  Found limit: {final_limit:,}, verifying...")
                    verify_payload = {
                        "model": model,
                        "messages": [{"role": "user", "content": "Hi"}],
                        "max_tokens": final_limit,
                        "temperature": 0.0,
                    }
                    verify_resp = await client.post(
                        url, json=verify_payload, headers=headers, timeout=60.0
                    )
                    if verify_resp.status_code == 200:
                        return final_limit
                    return final_limit - 1000

                if response.status_code == 429:
                    print(
                        f"  ⏳ Rate limited, waiting 62s... (attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(62)
                    continue

                # Parse max_total_tokens from error message
                error_text = response.text
                match = re.search(r"max_total_tokens=(\d+)", error_text)
                if match:
                    return int(match.group(1))

                # Alternative pattern
                match = re.search(r"max_model_len[^\d]*(\d+)", error_text)
                if match:
                    return int(match.group(1))

                print(f"  Could not parse limit from: {error_text}")
                return None

            except Exception as e:
                print(f"  Error: {e}")
                return None

        print("  Max retries exceeded")
        return None


async def verify_limit(api_key: str, model: str, limit: int) -> bool:
    """Verify the limit actually works."""
    url = "https://integrate.api.nvidia.com/v1/chat/completions"

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Say OK"}],
        "max_tokens": limit,
        "temperature": 0.0,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as client:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = await client.post(
                    url, json=payload, headers=headers, timeout=60.0
                )

                if response.status_code == 200:
                    return True

                if response.status_code == 429:
                    print(
                        f"  ⏳ Rate limited, waiting 62s... (attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(62)
                    continue

                return False

            except Exception as e:
                print(f"  Error: {e}")
                return False

        return False


async def main():
    api_key = os.environ.get("NVIDIA_NIM_API_KEY", "")
    if not api_key:
        print("Error: NVIDIA_NIM_API_KEY environment variable not set")
        sys.exit(1)

    model_env = os.environ.get("MODEL", "z-ai/glm5")
    default_model = model_env.split("/", 1)[-1] if "/" in model_env else model_env

    models_to_test = [default_model, "moonshotai/kimi-k2-instruct-0905"]

    print("NVIDIA NIM Token Limit Discovery")
    print("=" * 50)

    results = {}
    for model in models_to_test:
        print(f"\n📋 Model: {model}")

        limit = await get_token_limit(api_key, model)
        if limit:
            print(f"  Reported limit: {limit:,} tokens")
            print("  Verifying...", end=" ")
            verified = await verify_limit(api_key, model, limit)
            if verified:
                print("✅ Confirmed!")
            else:
                # Try limit - 100
                print(f"❌ Failed, trying {limit - 100:,}...")
                verified = await verify_limit(api_key, model, limit - 100)
                if verified:
                    limit = limit - 100
                    print(f"  ✅ Works at {limit:,}")

            results[model] = limit if verified else None
        else:
            results[model] = None

        if model != models_to_test[-1]:
            print("\n  Waiting 5s before next model...")
            await asyncio.sleep(5)

    print("\n" + "=" * 50)
    print("RESULTS")
    print("=" * 50)
    for model, limit in results.items():
        if limit:
            print(f"  {model}: {limit:,} tokens")
        else:
            print(f"  {model}: Could not determine")


if __name__ == "__main__":
    asyncio.run(main())
