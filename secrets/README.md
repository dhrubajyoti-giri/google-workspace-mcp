# Secrets directory

Place the following files here **before starting** the bridge:

1. `client_secret.json`  — Google Cloud OAuth 2.0 Web Application credentials
   (downloaded from Google Cloud Console → APIs & Services → Credentials)

This directory is mounted into the container at `/config` and is **never**
baked into the Docker image or committed to Git.
