#
# Builds both directions of the Latin <-> Icelandic dictionary
#
# NOTE: build_dict.sh wipes the whole ./objects/ directory on every
# invocation, so each bundle must be installed immediately after it's built
# rather than building both and installing at the end (the second build
# would delete the first bundle's output first).

.PHONY: all install clean

DICT_DEV_KIT_OBJ_DIR = /Applications/XcodeAdditionalTools/Utilities/DictionaryDevelopmentKit
DICT_BUILD_TOOL_DIR  = $(DICT_DEV_KIT_OBJ_DIR)/bin
BUILD_DICT_XML       = $(DICT_BUILD_TOOL_DIR)/build_dict.sh

MIN_OS_VERSION   = 10.6
export MACOSX_DEPLOYMENT_TARGET=$(MIN_OS_VERSION)
DICT_BUILD_DIR   = ./objects
INSTALL_DIR      = $(HOME)/Library/Dictionaries

# la2is: Latin headword -> Icelandic glossary + English (L&S) reference
LA2IS_NAME = LatinIcelandicDictionary
LA2IS_SRC  = LatinIcelandicDictionary

# is2la: Icelandic headword -> Latin word(s), reverse index (no morphology)
IS2LA_NAME = IcelandicLatinDictionary
IS2LA_SRC  = IcelandicLatinDictionary

all: install

install:
	@mkdir -p "$(INSTALL_DIR)"
	@echo "Compiling Latin -> Icelandic..."
	"$(BUILD_DICT_XML)" "$(LA2IS_NAME)" "$(LA2IS_SRC).xml" "$(LA2IS_SRC).css" "$(LA2IS_SRC).plist"
	rm -rf "$(INSTALL_DIR)/$(LA2IS_NAME).dictionary"
	cp -R "objects/$(LA2IS_NAME).dictionary" "$(INSTALL_DIR)/"
	@echo "Compiling Icelandic -> Latin (reverse index)..."
	"$(BUILD_DICT_XML)" "$(IS2LA_NAME)" "$(IS2LA_SRC).xml" "$(LA2IS_SRC).css" "$(IS2LA_SRC).plist"
	rm -rf "$(INSTALL_DIR)/$(IS2LA_NAME).dictionary"
	cp -R "objects/$(IS2LA_NAME).dictionary" "$(INSTALL_DIR)/"
	@echo "Installed both bundles successfully! Refresh your Dictionary app Settings."

clean:
	@echo "Cleaning up build artifacts..."
	$(RM) -rf $(DICT_BUILD_DIR)
