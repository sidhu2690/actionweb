name: 🌐 Live Server

on:
  workflow_dispatch:          # manual start button
  schedule:
    - cron: "0 */6 * * *"    # every 6 h → 00:00 06:00 12:00 18:00 UTC

# only one instance at a time
concurrency:
  group: live-server
  cancel-in-progress: false

jobs:
  serve:
    runs-on: ubuntu-latest
    timeout-minutes: 360      # max 6 h

    steps:
      # ── repo + python ──
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -q flask

      # ── start flask ──
      - name: Start server
        run: |
          python server.py &
          # wait until it's ready
          for i in $(seq 1 15); do
            curl -s http://localhost:8080 > /dev/null && break
            sleep 1
          done
          echo "✅ Flask is up on :8080"

      # ── open tunnel ──
      - name: Start tunnel
        env:
          NGROK_AUTHTOKEN: ${{ secrets.NGROK_AUTHTOKEN }}
          NGROK_DOMAIN:    ${{ vars.NGROK_DOMAIN }}
        run: |
          if [ -n "$NGROK_AUTHTOKEN" ] && [ -n "$NGROK_DOMAIN" ]; then
            # ━━ OPTION A: ngrok (stable URL) ━━
            echo "📡 ngrok → https://$NGROK_DOMAIN"
            curl -sO https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz
            tar xzf ngrok-v3-stable-linux-amd64.tgz
            ./ngrok config add-authtoken "$NGROK_AUTHTOKEN"
            ./ngrok http 8080 --domain="$NGROK_DOMAIN" --log=stdout > /tmp/tunnel.log 2>&1 &
            sleep 3
            echo "==========================================="
            echo "🌐  https://$NGROK_DOMAIN"
            echo "==========================================="

          else
            # ━━ OPTION B: cloudflared (zero-setup, URL in logs) ━━
            echo "📡 cloudflared quick tunnel..."
            curl -sL -o cloudflared \
              https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
            chmod +x cloudflared
            ./cloudflared tunnel --url http://localhost:8080 \
              --no-autoupdate > /tmp/tunnel.log 2>&1 &
            # wait for URL to appear
            for i in $(seq 1 30); do
              URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/tunnel.log | head -1)
              [ -n "$URL" ] && break
              sleep 1
            done
            echo "==========================================="
            echo "🌐  $URL"
            echo "==========================================="
            echo ""
            echo "⚠️  URL changes every run."
            echo "   Add NGROK_AUTHTOKEN secret + NGROK_DOMAIN"
            echo "   variable for a permanent URL (see README)."
          fi

      # ── stay alive ──
      - name: Keep alive
        run: |
          echo "Server is live. Sleeping 5h 55m..."
          sleep 21300
          echo "⏰ Time's up. Next run starts on schedule."
