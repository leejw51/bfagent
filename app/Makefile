APP_NAME    := hello
VERSION     := 0.3.0

BINARY      := bfagent
ENTRY       := program:main

BINARY_CLI  := bfagent-cli
ENTRY_CLI   := cli:main

TAURI_DIR   := tauri
TAURI_BIN   := bfagent-app

PYTHON      ?= python3

DIST_DIR        := dist
BIN_DIR         := bin
PYAPP_ROOT      := build/pyapp
PYAPP_ROOT_CLI  := build/pyapp-cli
WHEEL           := $(DIST_DIR)/$(APP_NAME)-$(VERSION)-py3-none-any.whl

APP_BUNDLE      := bfagent.app
APP_BUNDLE_ID   := com.bfagent.app

PIDFILE     := /tmp/bfagent.pid

SOURCES := pyproject.toml hello.py program.py backend.py frontend.py schema.py cli.py session.py functioncall.py gemma.capnp

.PHONY: help all wheel package package-py package-cli package-tauri app run debug client install format stop clean distclean

PYAPP_CACHE := $(HOME)/Library/Application Support/pyapp/hello

.DEFAULT_GOAL := help

help:
	@echo ""
	@echo "  bfagent — Gemma 4 agent (Cap'n Proto backend + Gradio frontend + Tauri shell)"
	@echo ""
	@echo "  Build:"
	@echo "    make package         build ./$(BINARY), ./$(BINARY_CLI), and ./$(TAURI_BIN)"
	@echo "    make package-py      build only ./$(BINARY) (Python pyapp)"
	@echo "    make package-cli     build only ./$(BINARY_CLI) (terminal client pyapp)"
	@echo "    make package-tauri   build only ./$(TAURI_BIN) (native window wrapper)"
	@echo "    make app             package, then wrap all three binaries in ./$(APP_BUNDLE)"
	@echo "    make wheel           build the Python wheel only"
	@echo ""
	@echo "  Run:"
	@echo "    make run             package, then launch ./$(TAURI_BIN) (native window)"
	@echo "    make debug           fast path: run backend+frontend in current $(PYTHON) — no rust build"
	@echo "    make client          run cli.py in current $(PYTHON) — defaults to repl, pass ARGS=... to override"
	@echo "    make install         conda install runtime deps into the active env (pip for mlx)"
	@echo "    make format          format Python sources with black"
	@echo "    make stop            kill running bfagent + bfagent-app"
	@echo ""
	@echo "  Clean:"
	@echo "    make clean           remove build outputs, binaries, and pyapp launch cache"
	@echo "    make distclean       also remove pyapp + tauri build caches"
	@echo ""
	@echo "  Standalone:"
	@echo "    ./$(BINARY)          run headless (browser at the printed ui-url)"
	@echo "    ./$(BINARY_CLI)      chat from the terminal (needs a backend already running)"
	@echo "    ./$(TAURI_BIN)       run as one program (opens native window)"
	@echo ""

all: package

wheel: $(WHEEL)

$(WHEEL): $(SOURCES)
	$(PYTHON) -m pip install --quiet --upgrade build
	$(PYTHON) -m build --wheel --outdir $(DIST_DIR)

# Build all three: the Python pyapp binary, the terminal CLI pyapp binary,
# and the Tauri native wrapper.
#
# After building, wipe the pyapp launch cache. PyApp keys its extract-and-
# install cache by (APP_NAME, version), so a same-version rebuild whose
# wheel content has changed (e.g. a newly added module) would otherwise
# silently boot from the previous install — yielding ModuleNotFoundError
# or other stale-source failures. Wiping here forces a fresh extraction
# on the next binary launch; pip's wheel cache (~/.cache/pip) still
# saves the dep download cost.
package: package-py package-cli package-tauri
	@echo ""
	@echo "wiping pyapp launch cache so the new binaries re-extract on first run..."
	@rm -rf "$(PYAPP_CACHE)"
	@mkdir -p $(BIN_DIR)
	@cp $(BINARY) $(BINARY_CLI) $(TAURI_BIN) $(BIN_DIR)/
	@echo ""
	@echo "built ./$(BINARY), ./$(BINARY_CLI), and ./$(TAURI_BIN)"
	@echo "copied binaries to ./$(BIN_DIR)/"
	@echo "run: ./$(TAURI_BIN)    # one-program experience (opens native window)"
	@echo "or:  ./$(BINARY)       # headless mode (browser at the printed ui-url)"
	@echo "or:  ./$(BINARY_CLI)   # terminal chat client (needs a backend running)"

package-py: $(BINARY)

$(BINARY): $(WHEEL)
	PYAPP_PROJECT_NAME=$(APP_NAME) \
	PYAPP_PROJECT_VERSION=$(VERSION) \
	PYAPP_PROJECT_PATH=$(abspath $(WHEEL)) \
	PYAPP_EXEC_SPEC=$(ENTRY) \
	cargo install pyapp --force --root $(PYAPP_ROOT)
	cp $(PYAPP_ROOT)/bin/pyapp $(BINARY)

# Same wheel as package-py, different entry point + cargo install root so
# pyapp produces a second binary that boots into cli:main instead of
# program:main. The Python runtime + dep cache it extracts on first run is
# shared across pyapp instances of the same APP_NAME, so the CLI doesn't
# pay the model-deps download twice.
package-cli: $(BINARY_CLI)

$(BINARY_CLI): $(WHEEL)
	PYAPP_PROJECT_NAME=$(APP_NAME) \
	PYAPP_PROJECT_VERSION=$(VERSION) \
	PYAPP_PROJECT_PATH=$(abspath $(WHEEL)) \
	PYAPP_EXEC_SPEC=$(ENTRY_CLI) \
	cargo install pyapp --force --root $(PYAPP_ROOT_CLI)
	cp $(PYAPP_ROOT_CLI)/bin/pyapp $(BINARY_CLI)

package-tauri: $(TAURI_BIN)

$(TAURI_BIN): $(TAURI_DIR)/Cargo.toml $(TAURI_DIR)/src/main.rs $(TAURI_DIR)/tauri.conf.json $(TAURI_DIR)/build.rs $(TAURI_DIR)/icons/icon.png
	cd $(TAURI_DIR) && cargo build --release
	cp $(TAURI_DIR)/target/release/$(TAURI_BIN) $(TAURI_BIN)

# Wrap all three binaries in a single .app bundle. macOS treats the whole
# bundle as one signing/staple unit, which lets a downloaded .app survive
# Finder's Gatekeeper UX even when copied out of a DMG — raw Mach-O can't
# carry a stapled notary ticket, but a bundle can.
#
# Layout:
#   $(APP_BUNDLE)/Contents/Info.plist
#   $(APP_BUNDLE)/Contents/MacOS/$(TAURI_BIN)   ← CFBundleExecutable (GUI entry)
#   $(APP_BUNDLE)/Contents/MacOS/$(BINARY)      ← embedded for terminal use
#   $(APP_BUNDLE)/Contents/MacOS/$(BINARY_CLI)  ← embedded for terminal use
#   $(APP_BUNDLE)/Contents/Resources/icon.png
define INFO_PLIST_BODY
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>          <string>$(APP_BUNDLE_ID)</string>
  <key>CFBundleName</key>                <string>$(BINARY)</string>
  <key>CFBundleDisplayName</key>         <string>$(BINARY)</string>
  <key>CFBundleExecutable</key>          <string>$(TAURI_BIN)</string>
  <key>CFBundleVersion</key>             <string>$(VERSION)</string>
  <key>CFBundleShortVersionString</key>  <string>$(VERSION)</string>
  <key>CFBundlePackageType</key>         <string>APPL</string>
  <key>LSMinimumSystemVersion</key>      <string>11.0</string>
  <key>NSHighResolutionCapable</key>     <true/>
</dict>
</plist>
endef
export INFO_PLIST_BODY

app: package
	@echo "[app] assembling $(APP_BUNDLE)..."
	@rm -rf $(APP_BUNDLE)
	@mkdir -p $(APP_BUNDLE)/Contents/MacOS $(APP_BUNDLE)/Contents/Resources
	@cp $(BIN_DIR)/$(BINARY)     $(APP_BUNDLE)/Contents/MacOS/$(BINARY)
	@cp $(BIN_DIR)/$(BINARY_CLI) $(APP_BUNDLE)/Contents/MacOS/$(BINARY_CLI)
	@cp $(BIN_DIR)/$(TAURI_BIN)  $(APP_BUNDLE)/Contents/MacOS/$(TAURI_BIN)
	@chmod +x $(APP_BUNDLE)/Contents/MacOS/*
	@cp $(TAURI_DIR)/icons/icon.png $(APP_BUNDLE)/Contents/Resources/icon.png
	@printf '%s\n' "$$INFO_PLIST_BODY" > $(APP_BUNDLE)/Contents/Info.plist
	@plutil -lint $(APP_BUNDLE)/Contents/Info.plist >/dev/null
	@echo ""
	@echo "built ./$(APP_BUNDLE)"
	@echo "  GUI:    open ./$(APP_BUNDLE)         # or double-click in Finder"
	@echo "  CLI:    ./$(APP_BUNDLE)/Contents/MacOS/$(BINARY_CLI)"
	@echo "  agent:  ./$(APP_BUNDLE)/Contents/MacOS/$(BINARY)"

run: package
	./$(TAURI_BIN)

# Run the CLI in the active interpreter without packaging. Pass args via
# ARGS=... e.g.  make client ARGS="ping"  or  make client ARGS="chat 'hi'"
# With no ARGS, drop straight into the interactive REPL — the typical
# second-terminal use case after `make debug`.
client:
	@$(PYTHON) cli.py $(if $(strip $(ARGS)),$(ARGS),repl)

# Fast iteration loop: skip the wheel/pyapp/Tauri builds and just run the
# Python orchestrator (program.py) under whatever interpreter $(PYTHON)
# resolves to — typically the active conda/venv. program.py itself spawns
# the Cap'n Proto backend in a daemon thread, waits for the port, then
# launches the Gradio frontend in the foreground.
debug:
	@echo "[debug] python = $$($(PYTHON) -c 'import sys; print(sys.executable)')"
	@echo "[debug] cwd    = $$(pwd)"
	@echo "[debug] env    = $${CONDA_DEFAULT_ENV:-$${VIRTUAL_ENV:-system}}"
	$(PYTHON) program.py

# Install runtime deps into the active conda env. Two installers:
#   - conda install -c conda-forge: pycapnp + huggingface_hub (first-class
#     conda builds, faster than pip for the C-extension cases).
#   - pip:                          gradio (pulls many transitives like pytz
#                                   that the conda-forge gradio bundle
#                                   doesn't always carry) plus the
#                                   Apple-Silicon-only mlx wheels.
# The wheel build deps (build, setuptools) only matter for `make wheel`, so
# they're skipped here — `make wheel` installs them on demand.
install:
	@command -v conda >/dev/null 2>&1 || { \
	  echo "[install] conda not found in PATH — activate your anaconda env first"; \
	  exit 1; \
	}
	@echo "[install] conda env = $${CONDA_DEFAULT_ENV:-base}"
	conda install -y -c conda-forge pycapnp huggingface_hub
	$(PYTHON) -m pip install --upgrade gradio mlx mlx-lm mlx-vlm

# Format Python sources with black. Runs black against the current directory
# and lets it skip the usual build/cache trees via its default excludes.
format:
	@command -v black >/dev/null 2>&1 || $(PYTHON) -m pip install --quiet black
	$(PYTHON) -m black .

stop:
	@if [ -f $(PIDFILE) ]; then \
	  pid=$$(cat $(PIDFILE) 2>/dev/null); \
	  if [ -n "$$pid" ] && kill -0 $$pid 2>/dev/null; then \
	    echo "stopping bfagent pid=$$pid"; \
	    kill -TERM $$pid 2>/dev/null || true; \
	    sleep 1; \
	    kill -KILL $$pid 2>/dev/null || true; \
	  fi; \
	  rm -f $(PIDFILE); \
	fi
	-@pkill -TERM -f "$(TAURI_BIN)" 2>/dev/null || true
	-@pkill -TERM -f "python.*program\.py" 2>/dev/null || true
	-@pkill -TERM -f "from backend import main" 2>/dev/null || true
	@sleep 1
	-@pkill -KILL -f "$(TAURI_BIN)" 2>/dev/null || true
	-@pkill -KILL -f "python.*program\.py" 2>/dev/null || true
	-@pkill -KILL -f "from backend import main" 2>/dev/null || true
	@echo "stopped."

clean:
	rm -rf $(DIST_DIR) build *.egg-info $(BINARY) $(BINARY_CLI) $(TAURI_BIN) $(BIN_DIR) $(APP_BUNDLE)
	@rm -rf "$(PYAPP_CACHE)"

distclean: clean
	rm -rf $(PYAPP_ROOT) $(PYAPP_ROOT_CLI) $(TAURI_DIR)/target
