FROM nginx:1.20.2-alpine-perl

RUN apk update && apk add --no-cache \
    inotify-tools \
    && rm -rf /var/cache/apk/*
COPY ./nginx.sh /nginx.sh
RUN chmod +x /nginx.sh
CMD [ "/nginx.sh" ]