#!/bin/bash
BACKUP_DIR="/home/ubuntu/ai-team/platform/data/hourly_backup"
DB_FILE="/home/ubuntu/ai-team/platform/data/listings.db"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/listings_${TIMESTAMP}.db"

# Create backup
cp "${DB_FILE}" "${BACKUP_FILE}"

# Keep only last 48 hours (48 backups, one per hour)
ls -t ${BACKUP_DIR}/listings_*.db | tail -n +49 | xargs -r rm -f

echo "Backup created: ${BACKUP_FILE} at $(date)"
