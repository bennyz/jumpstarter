#!/bin/bash
set -euxv

# accepted parameters are:
# $1: driver name
# $2: driver class
# $3: author name
# $4: author email

# check if the number of parameters is correct
if [ "$#" -ne 4 ]; then
    echo "Illegal number of parameters"
    echo "Usage: create_driver.sh <driver_name> <driver_class> <author_name> <author_email>"
    echo "Example: create_driver.sh mydriver MyDriver \"John Something\" john@somewhere.com"
    exit 1
fi

export DRIVER_NAME=$1
export DRIVER_CLASS=$2
export AUTHOR_NAME=$3
export AUTHOR_EMAIL=$4

# create the driver directory
DRIVER_DIRECTORY=packages/jumpstarter-driver-${DRIVER_NAME}
MODULE_DIRECTORY=${DRIVER_DIRECTORY}/jumpstarter_driver_${DRIVER_NAME}
# create the module directories
mkdir -p ${MODULE_DIRECTORY}
mkdir -p ${DRIVER_DIRECTORY}/examples

# Define paths
DOCS_DIRECTORY=docs/source/api-reference/drivers
DOC_FILE=${DOCS_DIRECTORY}/${DRIVER_NAME}.md
README_FILE=${DRIVER_DIRECTORY}/README.md

# Create README.md file with initial documentation
echo "Creating README.md file: ${README_FILE}"
cat > "${README_FILE}" << 'EOF'
# ${DRIVER_CLASS} Driver

`jumpstarter-driver-${DRIVER_NAME}` provides functionality for interacting with ${DRIVER_NAME} devices.

## Installation

```bash
pip install jumpstarter-driver-${DRIVER_NAME}
```

## Configuration

Example configuration:

```yaml
interfaces:
  ${DRIVER_NAME}:
    driver: jumpstarter_driver_${DRIVER_NAME}.${DRIVER_CLASS}Driver
    parameters:
      # Add required parameters here
```

## API Reference

Add API documentation here.
EOF
# Need to expand variables after EOF to prevent early expansion
sed -i "s/\${DRIVER_CLASS}/${DRIVER_CLASS}/g; s/\${DRIVER_NAME}/${DRIVER_NAME}/g" "${README_FILE}"
echo "README.md file content:"
cat "${README_FILE}"

# Create symlink from documentation directory to README.md
mkdir -p ${DOCS_DIRECTORY}
echo "Creating symlink to README.md file"
rel_path=$(realpath --relative-to="${DOCS_DIRECTORY}" "${README_FILE}")
ln -sf "${rel_path}" "${DOC_FILE}"
echo "Created symlink: ${DOC_FILE} -> ${rel_path}"

for f in __init__.py client.py driver_test.py driver.py; do
    echo "Creating: ${MODULE_DIRECTORY}/${f}"
    envsubst < __templates__/driver/jumpstarter_driver/${f}.tmpl > ${MODULE_DIRECTORY}/${f}
done

for f in .gitignore pyproject.toml examples/exporter.yaml; do
    echo "Creating: ${DRIVER_DIRECTORY}/${f}"
    envsubst < __templates__/driver/${f}.tmpl > ${DRIVER_DIRECTORY}/${f}
done
