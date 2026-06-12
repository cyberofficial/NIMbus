#!/usr/bin/env bash
# Build NIMbus Linux standalone executable

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== NIMbus Linux Build Script ===${NC}"
echo ""

# Check if we're in the right directory
if [ ! -f "packaged_entry.py" ]; then
    echo -e "${RED}Error: packaged_entry.py not found. Run from NIMbus project root.${NC}"
    exit 1
fi

# Check for pyinstaller
if ! command -v pyinstaller &> /dev/null; then
    echo -e "${YELLOW}Installing pyinstaller...${NC}"
    pip install pyinstaller
fi

# Download tiktoken cache if needed
echo -e "${GREEN}Downloading tiktoken cache...${NC}"
python build_exe_download_tiktoken.py

# Build using the Linux spec
echo -e "${GREEN}Building Linux executable with pyinstaller...${NC}"
pyinstaller --clean --distpath dist_linux nimbus_linux.spec

# Check if build succeeded
if [ -f "dist_linux/nimbus" ]; then
    echo ""
    echo -e "${GREEN}=== Build Successful! ===${NC}"
    echo -e "Executable: ${GREEN}dist_linux/nimbus${NC}"
    echo ""
    echo "Usage:"
    echo "  ./dist_linux/nimbus              # Start proxy server"
    echo "  ./dist_linux/nimbus --mcp        # Start MCP server (stdio)"
    echo "  ./dist_linux/nimbus --init       # Interactive setup wizard"
    echo "  ./dist_linux/nimbus --init restore  # Restore settings backup"
    echo ""
    # Make sure it's executable
    chmod +x dist_linux/nimbus

    # Quick test
    echo -e "${YELLOW}Testing executable...${NC}"
    ./dist_linux/nimbus --help 2>&1 | head -5 || true
else
    echo -e "${RED}Build failed - executable not found${NC}"
    exit 1
fi