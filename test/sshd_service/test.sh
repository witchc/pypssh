rm -rf  ~/.pypssh/inventory/inventory.yaml
docker-compose down
docker-compose up -d --scale sshd=100

# Wait for all running containers to pass their health checks
while read -r container_id; do
  echo "Waiting for container $container_id to pass its health check..."
  while [[ "$(docker inspect --format='{{.State.Health.Status}}' "$container_id")" != "healthy" ]]; do
    sleep 1
  done
done < <(docker ps --filter "health=healthy" --format "{{.ID}}")

echo "All containers have passed their health checks!"

docker ps -qf "ancestor=panubo/sshd" | xargs docker inspect --format='{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' | python3 ../../pypssh.py config add-host - -u root -p root -P 22 -t test=test

time python3 ../../pypssh.py -t test execute -c "(date && hostname -i | cut -d ' ' -f 1) | xargs"