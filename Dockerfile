FROM archlinux:latest

RUN pacman -Sy --noconfirm
RUN pacman -S reflector rsync --noconfirm
RUN reflector -l 5 --country US,Canada --sort rate --save /etc/pacman.d/mirrorlist

# Update the system and install dependencies
RUN pacman -Syu --noconfirm
RUN pacman -S --noconfirm base-devel git pacman-contrib python python-requests

# Entrypoint script
COPY entrypoint.py /entrypoint.py
RUN chmod +x /entrypoint.py

# Create a non-root user to build packages
RUN useradd -m builder

# Give the builder user passwordless sudo access
USER root
RUN echo "builder ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/builder && \
    chmod 0440 /etc/sudoers.d/builder

USER builder
WORKDIR /home/builder

ENTRYPOINT ["/entrypoint.py"]
