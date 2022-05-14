#!/bin/sh
while true; do
  inotifywait --exclude .crt -e create -e modify -e delete -e move /root/
  # Check NGINX Configuration Test
  # Only Reload NGINX If NGINX Configuration Test Pass
  nginx -t
  if [ $? -eq 0 ]
  then
    echo "Reloading Nginx Configuration"
    nginx
  fi
done