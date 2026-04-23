# Makefile for OpenViking

# Variables
PYTHON ?= python3
SETUP_PY := setup.py
AGFS_SERVER_DIR := third_party/agfs/agfs-server
OV_CLI_DIR := crates/ov_cli
MCP_PYTHON ?= $(CURDIR)/.venv/bin/python
BID_MATERIAL_MCP_DEBUG_SCRIPT := $(CURDIR)/scripts/bid_material_mcp_debug.py
BID_MATERIAL_MCP_CONFIG ?= $(HOME)/.openviking/ov-bidding.conf
BID_MATERIAL_MCP_PROTOCOL ?= line
BID_MATERIAL_MCP_OUTPUT ?= parsed
BID_MATERIAL_MCP_QUERY ?= ISO 证书
BID_MATERIAL_MCP_TARGET_URI ?= viking://resources/
BID_MATERIAL_MCP_TOP_K ?= 5
BID_MATERIAL_MCP_SECTION_NAME ?= 技术方案
BID_MATERIAL_MCP_REQUIREMENT ?= 请收集与数据库加密方案相关的可引用证据
BID_MATERIAL_MCP_TOOL ?= search_certificates
BID_MATERIAL_MCP_ARGS_JSON ?= {"query":"ISO 证书","target_uri":"viking://resources/","top_k":5}
BID_MATERIAL_MCP_GLOB_PATTERN ?= **/*.md
BID_MATERIAL_MCP_READ_URI ?= viking://resources/
BID_MATERIAL_MCP_READ_LEVEL ?= read
BID_MATERIAL_MCP_READ_INCLUDE_IMAGES ?= true
BID_MATERIAL_MCP_READ_MAX_IMAGES ?= 8

# Dependency Versions
MIN_PYTHON_VERSION := 3.10
MIN_GO_VERSION := 1.22
MIN_CMAKE_VERSION := 3.12
MIN_RUST_VERSION := 1.88
MIN_GCC_VERSION := 9
MIN_CLANG_VERSION := 11

# Output directories to clean
CLEAN_DIRS := \
	build/ \
	dist/ \
	*.egg-info/ \
	openviking/bin/ \
	openviking/lib/ \
	$(AGFS_SERVER_DIR)/build/ \
	$(OV_CLI_DIR)/target/ \
	src/cmake_build/ \
	.pytest_cache/ \
	.coverage \
	htmlcov/ \
	**/__pycache__/

.PHONY: all build clean help check-pip check-deps \
	bid-material-mcp-serve bid-material-mcp-list-tools bid-material-mcp-call \
	bid-material-mcp-cert bid-material-mcp-solution bid-material-mcp-evidence \
	bid-material-mcp-ov-search bid-material-mcp-ov-list bid-material-mcp-ov-glob \
	bid-material-mcp-ov-read bid-material-mcp-smoke

all: build

help:
	@echo "Available targets:"
	@echo "  build       - Build AGFS, ov CLI, and C++ extensions using setup.py"
	@echo "  clean       - Remove build artifacts and temporary files"
	@echo "  check-deps  - Check if required dependencies (Go, Rust, CMake, etc.) are installed"
	@echo "  bid-material-mcp-serve      - Start the bid-material MCP server over stdio"
	@echo "  bid-material-mcp-list-tools - Initialize the bid-material MCP server and print tools"
	@echo "  bid-material-mcp-call       - Call one MCP tool with BID_MATERIAL_MCP_TOOL/BID_MATERIAL_MCP_ARGS_JSON"
	@echo "                               Use BID_MATERIAL_MCP_OUTPUT=full for raw MCP envelopes"
	@echo "  bid-material-mcp-cert       - Debug search_certificates with BID_MATERIAL_MCP_QUERY"
	@echo "  bid-material-mcp-solution   - Debug search_solution_materials with BID_MATERIAL_MCP_QUERY"
	@echo "  bid-material-mcp-evidence   - Debug collect_bid_evidence with BID_MATERIAL_MCP_SECTION_NAME/BID_MATERIAL_MCP_REQUIREMENT"
	@echo "  bid-material-mcp-ov-search  - Debug openviking_search with BID_MATERIAL_MCP_QUERY"
	@echo "  bid-material-mcp-ov-list    - Debug openviking_list with BID_MATERIAL_MCP_TARGET_URI"
	@echo "  bid-material-mcp-ov-glob    - Debug openviking_glob with BID_MATERIAL_MCP_GLOB_PATTERN"
	@echo "  bid-material-mcp-ov-read    - Debug openviking_read with BID_MATERIAL_MCP_READ_URI"
	@echo "  bid-material-mcp-smoke      - Run list-tools first, then one MCP tool call as a smoke test"
	@echo "  help        - Show this help message"

check-pip:
	@if command -v uv > /dev/null 2>&1 && uv pip --help > /dev/null 2>&1; then \
		echo "  [OK] uv pip found"; \
	elif $(PYTHON) -m pip --version > /dev/null 2>&1; then \
		echo "  [OK] pip found"; \
	else \
		echo "Error: Neither uv pip nor pip found for $(PYTHON)."; \
		echo "Try fixing your environment by running:"; \
		echo "  uv sync          # if using uv"; \
		echo "  or"; \
		echo "  $(PYTHON) -m ensurepip --upgrade"; \
		exit 1; \
	fi

check-deps:
	@echo "Checking dependencies..."
	@# Python check
	@$(PYTHON) -c "import sys; v=sys.version_info; exit(0 if v.major > 3 or (v.major == 3 and v.minor >= 10) else 1)" || (echo "Error: Python >= $(MIN_PYTHON_VERSION) is required."; exit 1)
	@echo "  [OK] Python $$( $(PYTHON) -V | cut -d' ' -f2 )"
	@# Go check
	@command -v go > /dev/null 2>&1 || (echo "Error: Go is not installed."; exit 1)
	@GO_VER=$$(go version | awk '{print $$3}' | sed 's/go//'); \
	$(PYTHON) -c "v='$$GO_VER'.split('.'); exit(0 if int(v[0]) > 1 or (int(v[0]) == 1 and int(v[1]) >= 22) else 1)" || (echo "Error: Go >= $(MIN_GO_VERSION) is required. Found $$GO_VER"; exit 1); \
	echo "  [OK] Go $$GO_VER"
	@# CMake check
	@command -v cmake > /dev/null 2>&1 || (echo "Error: CMake is not installed."; exit 1)
	@CMAKE_VER=$$(cmake --version | head -n1 | awk '{print $$3}'); \
	$(PYTHON) -c "v='$$CMAKE_VER'.split('.'); exit(0 if int(v[0]) > 3 or (int(v[0]) == 3 and int(v[1]) >= 12) else 1)" || (echo "Error: CMake >= $(MIN_CMAKE_VERSION) is required. Found $$CMAKE_VER"; exit 1); \
	echo "  [OK] CMake $$CMAKE_VER"
	@# Rust check
	@command -v rustc > /dev/null 2>&1 || (echo "Error: Rust is not installed."; exit 1)
	@RUST_VER=$$(rustc --version | awk '{print $$2}'); \
	$(PYTHON) -c "v='$$RUST_VER'.split('.'); exit(0 if int(v[0]) > 1 or (int(v[0]) == 1 and int(v[1]) >= 88) else 1)" || (echo "Error: Rust >= $(MIN_RUST_VERSION) is required. Found $$RUST_VER"; exit 1); \
	echo "  [OK] Rust $$RUST_VER"
	@# C++ Compiler check
	@if command -v clang++ > /dev/null 2>&1; then \
		CLANG_VER_FULL=$$(clang++ --version | head -n1 | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | head -n1); \
		CLANG_VER=$$(echo $$CLANG_VER_FULL | cut -d. -f1); \
		if [ $$CLANG_VER -lt $(MIN_CLANG_VERSION) ]; then echo "Error: Clang >= $(MIN_CLANG_VERSION) is required. Found $$CLANG_VER_FULL"; exit 1; fi; \
		echo "  [OK] Clang $$CLANG_VER_FULL"; \
	elif command -v g++ > /dev/null 2>&1; then \
		GCC_VER_FULL=$$(g++ -dumpversion); \
		GCC_VER=$$(echo $$GCC_VER_FULL | cut -d. -f1); \
		if [ $$GCC_VER -lt $(MIN_GCC_VERSION) ]; then echo "Error: GCC >= $(MIN_GCC_VERSION) is required. Found $$GCC_VER_FULL"; exit 1; fi; \
		echo "  [OK] GCC $$GCC_VER_FULL"; \
	else \
		echo "Error: C++ compiler (GCC or Clang) is required."; exit 1; \
	fi

build: check-deps check-pip
	@echo "Starting build process via setup.py..."
	$(PYTHON) $(SETUP_PY) build_ext --inplace
	@if command -v uv > /dev/null 2>&1 && uv pip --help > /dev/null 2>&1; then \
		echo "  [OK] uv pip found, use uv pip to install..."; \
		uv pip install -e .; \
	else \
		echo "  [OK] pip found, use pip to install..."; \
		$(PYTHON) -m pip install -e .; \
	fi
	@echo "Build completed successfully."

clean:
	@echo "Cleaning up build artifacts..."
	@for dir in $(CLEAN_DIRS); do \
		if [ -d "$$dir" ] || [ -f "$$dir" ]; then \
			echo "Removing $$dir"; \
			rm -rf $$dir; \
		fi \
	done
	@find . -name "*.pyc" -delete
	@find . -name "__pycache__" -type d -exec rm -rf {} +
	@echo "Cleanup completed."

bid-material-mcp-serve:
	$(MCP_PYTHON) -m vikingbot bid-material-mcp -c "$(BID_MATERIAL_MCP_CONFIG)"

bid-material-mcp-list-tools:
	$(PYTHON) $(BID_MATERIAL_MCP_DEBUG_SCRIPT) \
		--python "$(MCP_PYTHON)" \
		--config "$(BID_MATERIAL_MCP_CONFIG)" \
		--protocol "$(BID_MATERIAL_MCP_PROTOCOL)" \
		--output "$(BID_MATERIAL_MCP_OUTPUT)" \
		list-tools

bid-material-mcp-call:
	$(PYTHON) $(BID_MATERIAL_MCP_DEBUG_SCRIPT) \
		--python "$(MCP_PYTHON)" \
		--config "$(BID_MATERIAL_MCP_CONFIG)" \
		--protocol "$(BID_MATERIAL_MCP_PROTOCOL)" \
		--output "$(BID_MATERIAL_MCP_OUTPUT)" \
		call \
		--tool "$(BID_MATERIAL_MCP_TOOL)" \
		--arguments-json '$(BID_MATERIAL_MCP_ARGS_JSON)'

bid-material-mcp-cert:
	$(PYTHON) $(BID_MATERIAL_MCP_DEBUG_SCRIPT) \
		--python "$(MCP_PYTHON)" \
		--config "$(BID_MATERIAL_MCP_CONFIG)" \
		--protocol "$(BID_MATERIAL_MCP_PROTOCOL)" \
		--output "$(BID_MATERIAL_MCP_OUTPUT)" \
		cert \
		--query "$(BID_MATERIAL_MCP_QUERY)" \
		--target-uri "$(BID_MATERIAL_MCP_TARGET_URI)" \
		--top-k "$(BID_MATERIAL_MCP_TOP_K)"

bid-material-mcp-solution:
	$(PYTHON) $(BID_MATERIAL_MCP_DEBUG_SCRIPT) \
		--python "$(MCP_PYTHON)" \
		--config "$(BID_MATERIAL_MCP_CONFIG)" \
		--protocol "$(BID_MATERIAL_MCP_PROTOCOL)" \
		--output "$(BID_MATERIAL_MCP_OUTPUT)" \
		solution \
		--query "$(BID_MATERIAL_MCP_QUERY)" \
		--target-uri "$(BID_MATERIAL_MCP_TARGET_URI)" \
		--top-k "$(BID_MATERIAL_MCP_TOP_K)"

bid-material-mcp-evidence:
	$(PYTHON) $(BID_MATERIAL_MCP_DEBUG_SCRIPT) \
		--python "$(MCP_PYTHON)" \
		--config "$(BID_MATERIAL_MCP_CONFIG)" \
		--protocol "$(BID_MATERIAL_MCP_PROTOCOL)" \
		--output "$(BID_MATERIAL_MCP_OUTPUT)" \
		evidence \
		--section-name "$(BID_MATERIAL_MCP_SECTION_NAME)" \
		--requirement "$(BID_MATERIAL_MCP_REQUIREMENT)" \
		--target-uri "$(BID_MATERIAL_MCP_TARGET_URI)" \
		--top-k "$(BID_MATERIAL_MCP_TOP_K)"

bid-material-mcp-ov-search:
	$(PYTHON) $(BID_MATERIAL_MCP_DEBUG_SCRIPT) \
		--python "$(MCP_PYTHON)" \
		--config "$(BID_MATERIAL_MCP_CONFIG)" \
		--protocol "$(BID_MATERIAL_MCP_PROTOCOL)" \
		--output "$(BID_MATERIAL_MCP_OUTPUT)" \
		ov-search \
		--query "$(BID_MATERIAL_MCP_QUERY)" \
		--target-uri "$(BID_MATERIAL_MCP_TARGET_URI)"

bid-material-mcp-ov-list:
	$(PYTHON) $(BID_MATERIAL_MCP_DEBUG_SCRIPT) \
		--python "$(MCP_PYTHON)" \
		--config "$(BID_MATERIAL_MCP_CONFIG)" \
		--protocol "$(BID_MATERIAL_MCP_PROTOCOL)" \
		--output "$(BID_MATERIAL_MCP_OUTPUT)" \
		ov-list \
		--uri "$(BID_MATERIAL_MCP_TARGET_URI)"

bid-material-mcp-ov-glob:
	$(PYTHON) $(BID_MATERIAL_MCP_DEBUG_SCRIPT) \
		--python "$(MCP_PYTHON)" \
		--config "$(BID_MATERIAL_MCP_CONFIG)" \
		--protocol "$(BID_MATERIAL_MCP_PROTOCOL)" \
		--output "$(BID_MATERIAL_MCP_OUTPUT)" \
		ov-glob \
		--pattern '$(BID_MATERIAL_MCP_GLOB_PATTERN)' \
		--uri "$(BID_MATERIAL_MCP_TARGET_URI)"

bid-material-mcp-ov-read:
	$(PYTHON) $(BID_MATERIAL_MCP_DEBUG_SCRIPT) \
		--python "$(MCP_PYTHON)" \
		--config "$(BID_MATERIAL_MCP_CONFIG)" \
		--protocol "$(BID_MATERIAL_MCP_PROTOCOL)" \
		--output "$(BID_MATERIAL_MCP_OUTPUT)" \
		ov-read \
		--uri "$(BID_MATERIAL_MCP_READ_URI)" \
		--level "$(BID_MATERIAL_MCP_READ_LEVEL)" \
		--max-images "$(BID_MATERIAL_MCP_READ_MAX_IMAGES)" \
		$(if $(filter true,$(BID_MATERIAL_MCP_READ_INCLUDE_IMAGES)),--include-images,--no-include-images)

bid-material-mcp-smoke:
	$(MAKE) bid-material-mcp-list-tools
	$(MAKE) bid-material-mcp-call
