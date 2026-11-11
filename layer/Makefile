# Requisitos (Arch/CachyOS): sudo pacman -S vulkan-headers base-devel   (teste: vulkan-tools)
CXX      ?= g++
CXXFLAGS ?= -O2 -std=c++17 -fPIC -Wall -Wextra -fvisibility=hidden
LIB       = libVkLayer_fluxframe.so
LIBDIR    = $(HOME)/.local/lib/fluxframe
LAYERDIR  = $(HOME)/.local/share/vulkan/implicit_layer.d

all: $(LIB)

$(LIB): fluxframe_layer.cpp present_stats.h
	$(CXX) $(CXXFLAGS) -shared -o $@ $< -Wl,-z,defs -lpthread

install: $(LIB)
	mkdir -p $(LIBDIR) $(LAYERDIR)
	cp $(LIB) $(LIBDIR)/
	@printf '%s\n' \
	  '{' \
	  '  "file_format_version": "1.2.0",' \
	  '  "layer": {' \
	  '    "name": "VK_LAYER_FLUXFRAME_framegen",' \
	  '    "type": "GLOBAL",' \
	  '    "library_path": "$(LIBDIR)/$(LIB)",' \
	  '    "api_version": "1.3.0",' \
	  '    "implementation_version": "1",' \
	  '    "description": "FluxFrame: geracao de quadros como camada Vulkan",' \
	  '    "functions": { "vkNegotiateLoaderLayerInterfaceVersion": "vkNegotiateLoaderLayerInterfaceVersion" },' \
	  '    "enable_environment": { "ENABLE_FLUXFRAME": "1" },' \
	  '    "disable_environment": { "DISABLE_FLUXFRAME": "1" }' \
	  '  }' \
	  '}' > $(LAYERDIR)/VkLayer_fluxframe.json
	@echo "Instalado em $(LAYERDIR). Teste: ENABLE_FLUXFRAME=1 vkcube"

uninstall:
	rm -f $(LIBDIR)/$(LIB) $(LAYERDIR)/VkLayer_fluxframe.json

clean:
	rm -f $(LIB)

.PHONY: all install uninstall clean