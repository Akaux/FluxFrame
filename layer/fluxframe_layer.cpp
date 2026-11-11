// FluxFrame - camada Vulkan (ETAPA 1: passthrough + log)
//
// Carregada pelo loader do Vulkan dentro do processo do jogo. Nesta etapa NÃO altera nada:
// repassa todas as chamadas e registra (stderr) o que será necessário nas próximas etapas:
// dispositivo, formato/tamanho/modo de present da swapchain e o fps real do present.
//
// Ativar:  ENABLE_FLUXFRAME=1 <jogo>   (desativar: DISABLE_FLUXFRAME=1)
// Código próprio do FluxFrame; usa apenas a interface pública de camadas do Vulkan.
#include <vulkan/vulkan.h>
#include <vulkan/vk_layer.h>

#include "present_stats.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <unordered_map>

#define FF_EXPORT extern "C" __attribute__((visibility("default")))
#define FF_LOG(...) do { std::fprintf(stderr, "[fluxframe] " __VA_ARGS__); std::fputc('\n', stderr); } while (0)

namespace {

// ------------------------------------------------------------------ estado
struct InstanceData {
    VkInstance instance = VK_NULL_HANDLE;
    PFN_vkGetInstanceProcAddr gipa = nullptr;
    PFN_vkDestroyInstance DestroyInstance = nullptr;
    PFN_vkGetPhysicalDeviceProperties GetPhysicalDeviceProperties = nullptr;
};

struct DeviceData {
    PFN_vkGetDeviceProcAddr gdpa = nullptr;
    PFN_vkDestroyDevice DestroyDevice = nullptr;
    PFN_vkCreateSwapchainKHR CreateSwapchainKHR = nullptr;
    PFN_vkDestroySwapchainKHR DestroySwapchainKHR = nullptr;
    PFN_vkQueuePresentKHR QueuePresentKHR = nullptr;
};

std::mutex g_mutex;
std::unordered_map<void*, InstanceData> g_instances;   // chave = ponteiro de dispatch do loader
std::unordered_map<void*, DeviceData> g_devices;       // VkDevice e VkQueue compartilham a chave

// O primeiro campo de todo objeto despachável é o ponteiro da tabela do loader.
void* key(void* handle) { return *reinterpret_cast<void**>(handle); }

InstanceData* instance_of(void* handle) {
    std::lock_guard<std::mutex> lock(g_mutex);
    auto it = g_instances.find(key(handle));
    return it == g_instances.end() ? nullptr : &it->second;
}
DeviceData* device_of(void* handle) {
    std::lock_guard<std::mutex> lock(g_mutex);
    auto it = g_devices.find(key(handle));
    return it == g_devices.end() ? nullptr : &it->second;
}

// ------------------------------------------------------------------ instância
VKAPI_ATTR VkResult VKAPI_CALL ff_CreateInstance(const VkInstanceCreateInfo* ci,
        const VkAllocationCallbacks* alloc, VkInstance* out) {
    auto* link = reinterpret_cast<VkLayerInstanceCreateInfo*>(const_cast<void*>(ci->pNext));
    while (link && !(link->sType == VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO &&
                     link->function == VK_LAYER_LINK_INFO))
        link = reinterpret_cast<VkLayerInstanceCreateInfo*>(const_cast<void*>(link->pNext));
    if (!link) return VK_ERROR_INITIALIZATION_FAILED;

    PFN_vkGetInstanceProcAddr gipa = link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;       // avança a cadeia para a próxima camada

    auto create = reinterpret_cast<PFN_vkCreateInstance>(gipa(VK_NULL_HANDLE, "vkCreateInstance"));
    if (!create) return VK_ERROR_INITIALIZATION_FAILED;
    VkResult r = create(ci, alloc, out);
    if (r != VK_SUCCESS) return r;

    InstanceData d;
    d.instance = *out;
    d.gipa = gipa;
    d.DestroyInstance = reinterpret_cast<PFN_vkDestroyInstance>(gipa(*out, "vkDestroyInstance"));
    d.GetPhysicalDeviceProperties = reinterpret_cast<PFN_vkGetPhysicalDeviceProperties>(
        gipa(*out, "vkGetPhysicalDeviceProperties"));
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        g_instances[key(*out)] = d;
    }
    const char* app = ci->pApplicationInfo && ci->pApplicationInfo->pApplicationName
                          ? ci->pApplicationInfo->pApplicationName : "?";
    FF_LOG("instância criada (app: %s)", app);
    return VK_SUCCESS;
}

VKAPI_ATTR void VKAPI_CALL ff_DestroyInstance(VkInstance instance, const VkAllocationCallbacks* alloc) {
    InstanceData d;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        auto it = g_instances.find(key(instance));
        if (it == g_instances.end()) return;
        d = it->second;
        g_instances.erase(it);
    }
    d.DestroyInstance(instance, alloc);
}

// ------------------------------------------------------------------ dispositivo
VKAPI_ATTR VkResult VKAPI_CALL ff_CreateDevice(VkPhysicalDevice phys, const VkDeviceCreateInfo* ci,
        const VkAllocationCallbacks* alloc, VkDevice* out) {
    auto* link = reinterpret_cast<VkLayerDeviceCreateInfo*>(const_cast<void*>(ci->pNext));
    while (link && !(link->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO &&
                     link->function == VK_LAYER_LINK_INFO))
        link = reinterpret_cast<VkLayerDeviceCreateInfo*>(const_cast<void*>(link->pNext));
    if (!link) return VK_ERROR_INITIALIZATION_FAILED;

    InstanceData* inst = instance_of(phys);                 // VkPhysicalDevice usa a chave da instância
    if (!inst) return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr gipa = link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkGetDeviceProcAddr gdpa = link->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;

    auto create = reinterpret_cast<PFN_vkCreateDevice>(gipa(inst->instance, "vkCreateDevice"));
    if (!create) return VK_ERROR_INITIALIZATION_FAILED;
    VkResult r = create(phys, ci, alloc, out);
    if (r != VK_SUCCESS) return r;

    DeviceData d;
    d.gdpa = gdpa;
    d.DestroyDevice = reinterpret_cast<PFN_vkDestroyDevice>(gdpa(*out, "vkDestroyDevice"));
    d.CreateSwapchainKHR = reinterpret_cast<PFN_vkCreateSwapchainKHR>(gdpa(*out, "vkCreateSwapchainKHR"));
    d.DestroySwapchainKHR = reinterpret_cast<PFN_vkDestroySwapchainKHR>(gdpa(*out, "vkDestroySwapchainKHR"));
    d.QueuePresentKHR = reinterpret_cast<PFN_vkQueuePresentKHR>(gdpa(*out, "vkQueuePresentKHR"));
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        g_devices[key(*out)] = d;
    }
    if (inst->GetPhysicalDeviceProperties) {
        VkPhysicalDeviceProperties p{};
        inst->GetPhysicalDeviceProperties(phys, &p);
        FF_LOG("dispositivo criado: %s (Vulkan %u.%u)", p.deviceName,
               VK_VERSION_MAJOR(p.apiVersion), VK_VERSION_MINOR(p.apiVersion));
    }
    return VK_SUCCESS;
}

VKAPI_ATTR void VKAPI_CALL ff_DestroyDevice(VkDevice device, const VkAllocationCallbacks* alloc) {
    DeviceData d;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        auto it = g_devices.find(key(device));
        if (it == g_devices.end()) return;
        d = it->second;
        g_devices.erase(it);
    }
    d.DestroyDevice(device, alloc);
}

// ------------------------------------------------------------------ swapchain
VKAPI_ATTR VkResult VKAPI_CALL ff_CreateSwapchainKHR(VkDevice device, const VkSwapchainCreateInfoKHR* ci,
        const VkAllocationCallbacks* alloc, VkSwapchainKHR* out) {
    DeviceData* d = device_of(device);
    if (!d) return VK_ERROR_INITIALIZATION_FAILED;
    FF_LOG("swapchain: %ux%u formato %d, %u imagens (mín.), present mode %d, usage 0x%x",
           ci->imageExtent.width, ci->imageExtent.height, static_cast<int>(ci->imageFormat),
           ci->minImageCount, static_cast<int>(ci->presentMode), static_cast<unsigned>(ci->imageUsage));
    return d->CreateSwapchainKHR(device, ci, alloc, out);
}

VKAPI_ATTR void VKAPI_CALL ff_DestroySwapchainKHR(VkDevice device, VkSwapchainKHR sc,
        const VkAllocationCallbacks* alloc) {
    DeviceData* d = device_of(device);
    if (d) d->DestroySwapchainKHR(device, sc, alloc);
}

// ------------------------------------------------------------------ present (fps real)
VKAPI_ATTR VkResult VKAPI_CALL ff_QueuePresentKHR(VkQueue queue, const VkPresentInfoKHR* info) {
    DeviceData* d = device_of(queue);                       // a fila usa a mesma chave do device
    if (!d) return VK_ERROR_DEVICE_LOST;

    using clock = std::chrono::steady_clock;
    static std::mutex m;
    static PresentStats stats;
    static std::FILE* csv = nullptr;
    static bool csv_tried = false;
    {
        std::lock_guard<std::mutex> lock(m);
        if (!csv_tried) {                                   // FLUXFRAME_LOG_FILE=/caminho/fps.csv
            csv_tried = true;
            if (const char* path = std::getenv("FLUXFRAME_LOG_FILE")) {
                csv = std::fopen(path, "w");
                if (csv) std::fputs("t,jogo_fps,saida_fps,low1_fps,fg_on\n", csv);
                else FF_LOG("não consegui abrir %s", path);
            }
        }
        const double now = std::chrono::duration<double>(clock::now().time_since_epoch()).count();
        FpsRow row;
        if (stats.on_present(now, &row)) {
            FF_LOG("present: %.1f fps (1%% low %.0f, frametime médio %.1f ms, pior %.1f ms)",
                   row.fps, row.low1, row.avg_ms, row.max_ms);
            if (csv) {                                      // passthrough: jogo == saída, FG desligado
                std::fprintf(csv, "%.1f,%.1f,%.1f,%.1f,0\n", row.t, row.fps, row.fps, row.low1);
                std::fflush(csv);
            }
        }
    }
    return d->QueuePresentKHR(queue, info);
}

// ------------------------------------------------------------------ resolução de funções
VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL ff_GetDeviceProcAddr(VkDevice device, const char* name) {
    if (!std::strcmp(name, "vkGetDeviceProcAddr")) return reinterpret_cast<PFN_vkVoidFunction>(ff_GetDeviceProcAddr);
    if (!std::strcmp(name, "vkDestroyDevice")) return reinterpret_cast<PFN_vkVoidFunction>(ff_DestroyDevice);
    if (!std::strcmp(name, "vkCreateSwapchainKHR")) return reinterpret_cast<PFN_vkVoidFunction>(ff_CreateSwapchainKHR);
    if (!std::strcmp(name, "vkDestroySwapchainKHR")) return reinterpret_cast<PFN_vkVoidFunction>(ff_DestroySwapchainKHR);
    if (!std::strcmp(name, "vkQueuePresentKHR")) return reinterpret_cast<PFN_vkVoidFunction>(ff_QueuePresentKHR);
    DeviceData* d = device_of(device);
    return d && d->gdpa ? d->gdpa(device, name) : nullptr;
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL ff_GetInstanceProcAddr(VkInstance instance, const char* name) {
    if (!std::strcmp(name, "vkGetInstanceProcAddr")) return reinterpret_cast<PFN_vkVoidFunction>(ff_GetInstanceProcAddr);
    if (!std::strcmp(name, "vkCreateInstance")) return reinterpret_cast<PFN_vkVoidFunction>(ff_CreateInstance);
    if (!std::strcmp(name, "vkDestroyInstance")) return reinterpret_cast<PFN_vkVoidFunction>(ff_DestroyInstance);
    if (!std::strcmp(name, "vkCreateDevice")) return reinterpret_cast<PFN_vkVoidFunction>(ff_CreateDevice);
    // funções de dispositivo também podem ser pedidas pela instância
    for (const char* n : {"vkGetDeviceProcAddr", "vkDestroyDevice", "vkCreateSwapchainKHR",
                          "vkDestroySwapchainKHR", "vkQueuePresentKHR"})
        if (!std::strcmp(name, n)) return ff_GetDeviceProcAddr(VK_NULL_HANDLE, name);
    InstanceData* i = instance ? instance_of(instance) : nullptr;
    return i && i->gipa ? i->gipa(instance, name) : nullptr;
}

}  // namespace

// ------------------------------------------------------------------ entrada da camada
FF_EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkNegotiateLoaderLayerInterfaceVersion(
        VkNegotiateLayerInterface* iface) {
    if (!iface || iface->sType != LAYER_NEGOTIATE_INTERFACE_STRUCT) return VK_ERROR_INITIALIZATION_FAILED;
    if (iface->loaderLayerInterfaceVersion > 2) iface->loaderLayerInterfaceVersion = 2;
    iface->pfnGetInstanceProcAddr = ff_GetInstanceProcAddr;
    iface->pfnGetDeviceProcAddr = ff_GetDeviceProcAddr;
    iface->pfnGetPhysicalDeviceProcAddr = nullptr;
    return VK_SUCCESS;
}