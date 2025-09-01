FROM archlinux:latest

# Update the system and install dependencies
RUN pacman -Syu --noconfirm && \
    pacman -S --noconfirm base-devel git pacman-contrib

# Entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create a non-root user to build packages
RUN useradd -m builder

# Give the builder user passwordless sudo access
USER root
RUN echo "builder ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/builder && \
    chmod 0440 /etc/sudoers.d/builder

USER builder
WORKDIR /home/builder

ENTRYPOINT ["/entrypoint.sh"]
