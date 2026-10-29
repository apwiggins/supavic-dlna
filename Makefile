IMAGE_NAME ?= supavic-dlna-test
CONTAINER_NAME ?= supavic-dlna-container

.PHONY: help build run all clean

# Default target when running just 'make'
help: ## Show this help message
	@echo "=========================================="
	@echo " SupaVic DLNA Docker - Makefile Help      "
	@echo "=========================================="
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

build: ## Build the Docker image
	docker build -t $(IMAGE_NAME) .

run: ## Run the container (uses --net=host for UPnP/DLNA discovery)
	docker run --rm --net=host --name $(CONTAINER_NAME) $(IMAGE_NAME)

all: build run ## Build the image and immediately run it

clean: ## Remove the built Docker image
	docker rmi -f $(IMAGE_NAME) || true
