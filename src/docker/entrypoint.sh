#!/bin/bash
set -e

# Set root password from ACCESS_TOKEN
if [ -n "$ACCESS_TOKEN" ]; then
    echo "root:$ACCESS_TOKEN" | chpasswd
fi

# Install SSH public key if provided
if [ -n "$SSH_PUBLIC_KEY" ]; then
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    echo "$SSH_PUBLIC_KEY" > /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
fi

# Start SSH daemon
/usr/sbin/sshd

# Start the Flask application
exec python3 /opt/app.py
