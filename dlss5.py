"""
DLSS 5 prototype: Neural Lighting, Material Reconstruction, AI SR, Temporal Consistency.

Estágios (batem com o diagrama pedido):
  1. Screen Capture        -> feito pelo capture_wayland.py (já existe)
  2. Scene Analysis        -> integrado no LIGHTING_FRAG (luma, contraste, pseudo-depth, edges)
  3. AI Upscaling          -> MATERIAL_FRAG (multi-scale detail injection)
  4. Neural Lighting       -> LIGHTING_FRAG (SSAO aproximado + sombra de contato + luz indireta)
  5. Temporal Reconstruction -> TEMPORAL_FRAG (TAA já existente em overlay.py)
  6. Enhanced Frame        -> saída final

Sobre "Vulkan shaders": os shaders abaixo são GLSL 330 (OpenGL 3.3, funciona na Vega 7).
Portar para Vulkan/SPIR-V é mecânico — a lógica é idêntica, muda só o loader.
NCNN/Real-ESRGAN real precisaria de modelo treinado + ncnn-vulkan-python (~100+ ms/quadro na
Vega 7). A aproximação por shader roda em ~2-3 ms e entrega o look sem o custo.

Performance estimada (960x540, por quadro ÚNICO do jogo):
  - LIGHTING_FRAG:   ~25 taps  ~ 2.5 ms
  - MATERIAL_FRAG:   ~20 taps  ~ 2.0 ms
  - Total DLSS5:     ~4.5 ms × fps do jogo
  - Cache: cada frame é processado UMA vez, mesmo se exibido como original e como parent
    de dois frames gerados.
"""
import collections

# =========================================================================
# PASS 1 - Scene Analysis + Neural Lighting
#   - 3x3 luminance/contrast/edges (Sobel)
#   - pseudo-depth = darkness + vertical
#   - SSAO aproximado (ring 8)
#   - sombra de contato por edge
#   - luz indireta (bright-pass blur 8 taps)
#   - realce de contraste local
# =========================================================================
LIGHTING_FRAG = """#version 330 core
in vec2 uv;
out vec4 fragColor;
uniform sampler2D tex;
uniform vec2 size;
uniform float strength;
uniform float ao_strength;
uniform float indirect_strength;
uniform float contrast_strength;

vec3 T(vec2 p) { return texture(tex, p).rgb; }
float L(vec3 c) { return dot(c, vec3(0.299, 0.587, 0.114)); }

void main() {
    vec2 px = 1.0 / size;
    vec3 c[9];
    float l[9];
    for (int j = 0; j < 3; j++)
        for (int i = 0; i < 3; i++) {
            c[j*3+i] = T(uv + vec2(i-1, j-1) * px);
            l[j*3+i] = L(c[j*3+i]);
        }
    float lc = l[4];
    float mean = (l[0]+l[1]+l[2]+l[3]+l[5]+l[6]+l[7]+l[8]) / 8.0;
    float local_contrast = abs(lc - mean);

    // Sobel para edges
    float gx = (l[2]+2.0*l[5]+l[8]) - (l[0]+2.0*l[3]+l[6]);
    float gy = (l[6]+2.0*l[7]+l[8]) - (l[0]+2.0*l[1]+l[2]);
    float edge = sqrt(gx*gx + gy*gy);

    // SSAO aproximado: ring 8
    float ao = 0.0;
    const int RING = 8;
    for (int k = 0; k < RING; k++) {
        float ang = float(k) * 6.2831853 / float(RING);
        vec2 off = vec2(cos(ang), sin(ang)) * px * 6.0;
        float nd = L(T(uv + off));
        ao += max(0.0, nd - lc);
    }
    ao = (ao / float(RING)) * ao_strength;
    float ao_factor = 1.0 - clamp(ao * 2.5, 0.0, 0.5);

    // Sombra de contato
    float contact = 1.0 - clamp(edge * 2.0, 0.0, 0.25) * ao_strength;

    // Luz indireta: bright-pass blur
    vec3 bright = vec3(0.0);
    const int BS = 8;
    for (int k = 0; k < BS; k++) {
        float ang = float(k) * 6.2831853 / float(BS);
        vec2 off = vec2(cos(ang), sin(ang)) * px * 12.0;
        vec3 s = T(uv + off);
        float sl = L(s);
        bright += max(s - vec3(0.4), vec3(0.0)) * smoothstep(0.4, 1.0, sl);
    }
    bright /= float(BS);

    vec3 color = c[4];
    color *= ao_factor * contact;
    color += bright * 0.4 * indirect_strength;
    color += (color - vec3(mean)) * (0.3 + local_contrast * 2.0) * contrast_strength;

    color = mix(c[4], clamp(color, 0.0, 1.0), strength);
    fragColor = vec4(color, 1.0);
}
"""

# =========================================================================
# PASS 2 - Material Reconstruction + AI Super Resolution
#   - classificação heurística por cor/textura/luminância:
#     metal, pele, água, vegetação, madeira
#   - bônus específico por material
#   - multi-scale unsharp (AI SR "neural-like")
# =========================================================================
MATERIAL_FRAG = """#version 330 core
in vec2 uv;
out vec4 fragColor;
uniform sampler2D tex;
uniform sampler2D texFlow;
uniform vec2 size;
uniform float strength;
uniform float material_strength;
uniform float sr_strength;
uniform float has_flow;

vec3 T(vec2 p) { return texture(tex, p).rgb; }
float L(vec3 c) { return dot(c, vec3(0.299, 0.587, 0.114)); }

void main() {
    vec2 px = 1.0 / size;
    vec3 c[9];
    float l[9];
    for (int j = 0; j < 3; j++)
        for (int i = 0; i < 3; i++) {
            c[j*3+i] = T(uv + vec2(i-1, j-1) * px);
            l[j*3+i] = L(c[j*3+i]);
        }
    vec3 center = c[4];
    float lc = l[4];
    float mean = (l[0]+l[1]+l[2]+l[3]+l[5]+l[6]+l[7]+l[8]) / 8.0;
    float local_contrast = abs(lc - mean);

    float maxc = max(max(center.r, center.g), center.b);
    float minc = min(min(center.r, center.g), center.b);
    float sat = (maxc - minc) / max(maxc, 0.001);
    float warmth = center.r - center.b;
    float greenness = center.g - max(center.r, center.b);
    float blueness = center.b - max(center.r, center.g);

    // Classificação heurística
    float metal = smoothstep(0.55, 0.85, lc) * (1.0 - smoothstep(0.0, 0.3, sat))
                * (1.0 - smoothstep(0.02, 0.15, local_contrast));
    float skin = smoothstep(0.03, 0.12, warmth) * smoothstep(0.1, 0.25, sat)
               * smoothstep(0.85, 0.55, lc) * smoothstep(0.2, 0.4, lc);
    float water = smoothstep(0.05, 0.2, blueness) * (1.0 - smoothstep(0.05, 0.2, local_contrast))
                * smoothstep(0.1, 0.35, lc) * smoothstep(0.9, 0.6, lc);
    float veg = smoothstep(0.03, 0.15, greenness) * smoothstep(0.08, 0.2, local_contrast);
    float wood = smoothstep(0.02, 0.1, warmth) * smoothstep(0.15, 0.3, sat)
               * smoothstep(0.15, 0.3, local_contrast) * (1.0 - skin);

    vec3 enhanced = center;

    // Metal: boost especular
    float spec = smoothstep(0.6, 1.0, lc);
    enhanced += enhanced * metal * spec * 0.3 * material_strength;

    // Pele: suaviza + leve calor
    vec3 soft = (c[1] + c[3] + c[5] + c[7]) * 0.25;
    enhanced = mix(enhanced, soft, skin * 0.4 * material_strength);
    enhanced *= vec3(1.0, 1.0 - 0.015 * skin * material_strength,
                          1.0 - 0.025 * skin * material_strength);

    // Água: refração por flow
    if (has_flow > 0.5) {
        vec2 flow = texture(texFlow, uv).rg;
        if (length(flow) > 0.3) {
            vec2 warped = uv + flow * 0.0008 * water * material_strength;
            enhanced = mix(enhanced, T(warped), water * 0.35 * material_strength);
        }
    }

    // Vegetação + madeira: realce de detalhe
    vec3 detail = center - vec3(mean);
    enhanced += detail * veg * 0.5 * material_strength;
    enhanced += detail * wood * 0.25 * material_strength;

    // AI SR: multi-scale unsharp
    vec3 sm_detail = center - vec3(mean);
    vec3 mid_avg = vec3(0.0);
    for (int k = 0; k < 8; k++) {
        float ang = float(k) * 0.7853982;
        vec2 off = vec2(cos(ang), sin(ang)) * px * 4.0;
        mid_avg += T(uv + off);
    }
    mid_avg /= 8.0;
    vec3 mid_detail = center - mid_avg;

    float edge_guard = 1.0 / (1.0 + local_contrast * 20.0);
    enhanced += sm_detail * 0.4 * sr_strength * edge_guard;
    enhanced += mid_detail * 0.2 * sr_strength;

    enhanced = mix(center, enhanced, strength);
    fragColor = vec4(clamp(enhanced, 0.0, 1.0), 1.0);
}
"""

PASSTHROUGH_FRAG = """#version 330 core
in vec2 uv;
out vec4 fragColor;
uniform sampler2D tex;
void main() { fragColor = texture(tex, uv); }
"""


class DLSS5Pipeline:
    """Executa os 2 passes e mantém um cache LRU por identidade de frame.
    Cada frame único é processado no máximo UMA vez, mesmo que apareça como parent
    de vários frames gerados."""

    MAX_CACHE = 6

    def __init__(self):
        self.GL = None
        self.vao = 0
        self.default_fbo = 0
        self.p_lighting = None
        self.p_material = None
        self.p_passthrough = None
        self.tmp_fbo = 0
        self.zero_flow = None
        self.cache = collections.OrderedDict()

    def init_gl(self, GL, vao, default_fbo, compile_fn):
        self.GL = GL
        self.vao = vao
        self.default_fbo = default_fbo
        self.p_lighting = compile_fn(LIGHTING_FRAG)
        self.p_material = compile_fn(MATERIAL_FRAG)
        self.p_passthrough = compile_fn(PASSTHROUGH_FRAG)
        self.l_loc = {n: GL.glGetUniformLocation(self.p_lighting, n)
                      for n in ("tex", "size", "strength", "ao_strength",
                                "indirect_strength", "contrast_strength")}
        self.m_loc = {n: GL.glGetUniformLocation(self.p_material, n)
                      for n in ("tex", "texFlow", "size", "strength", "material_strength",
                                "sr_strength", "has_flow")}
        self.tmp_fbo = GL.glGenFramebuffers(1)

        # zero-flow 1x1 (evita ler sampler não vinculado quando não há flow)
        self.zero_flow = GL.glGenTextures(1)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.zero_flow)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RG16F, 1, 1, 0, GL.GL_RG,
                        GL.GL_FLOAT, __import__("numpy").zeros((1, 1, 2), dtype="float32"))

    def _make_tex(self, w, h):
        GL = self.GL
        tex = GL.glGenTextures(1)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB8, w, h, 0, GL.GL_BGR,
                        GL.GL_UNSIGNED_BYTE, None)
        return tex

    def _run_passes(self, src_tex, w, h, flow_tex, params):
        GL = self.GL
        scratch = self._make_tex(w, h)
        out = self._make_tex(w, h)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.tmp_fbo)
        GL.glViewport(0, 0, w, h)

        # Pass 1: Lighting
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                   GL.GL_TEXTURE_2D, scratch, 0)
        GL.glUseProgram(self.p_lighting)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, src_tex)
        GL.glUniform1i(self.l_loc["tex"], 0)
        GL.glUniform2f(self.l_loc["size"], float(w), float(h))
        GL.glUniform1f(self.l_loc["strength"], params["strength"])
        GL.glUniform1f(self.l_loc["ao_strength"], params["ao"])
        GL.glUniform1f(self.l_loc["indirect_strength"], params["indirect"])
        GL.glUniform1f(self.l_loc["contrast_strength"], params["contrast"])
        GL.glBindVertexArray(self.vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)

        # Pass 2: Material + SR
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                   GL.GL_TEXTURE_2D, out, 0)
        GL.glUseProgram(self.p_material)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, scratch)
        GL.glUniform1i(self.m_loc["tex"], 0)
        GL.glActiveTexture(GL.GL_TEXTURE1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, flow_tex if flow_tex is not None else self.zero_flow)
        GL.glUniform1i(self.m_loc["texFlow"], 1)
        GL.glUniform1f(self.m_loc["has_flow"], 1.0 if flow_tex is not None else 0.0)
        GL.glUniform2f(self.m_loc["size"], float(w), float(h))
        GL.glUniform1f(self.m_loc["strength"], params["strength"])
        GL.glUniform1f(self.m_loc["material_strength"], params["material"])
        GL.glUniform1f(self.m_loc["sr_strength"], params["sr"])
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)

        GL.glBindVertexArray(0)
        GL.glUseProgram(0)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.default_fbo)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glDeleteTextures([scratch])
        return out

    def get_processed(self, frame, src_tex, flow_tex, w, h, params):
        key = id(frame)
        hit = self.cache.get(key)
        if hit is not None:
            self.cache.move_to_end(key)
            return hit[1]
        tex = self._run_passes(src_tex, w, h, flow_tex, params)
        self.cache[key] = (frame, tex)  # segura ref p/ manter id() estável
        while len(self.cache) > self.MAX_CACHE:
            _, (_, old) = self.cache.popitem(last=False)
            self.GL.glDeleteTextures([old])
        return tex

    def clear_cache(self):
        if self.GL:
            for _, tex in self.cache.values():
                self.GL.glDeleteTextures([tex])
        self.cache.clear()