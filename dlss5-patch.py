cat > patch_dlss5.py <<'PYEOF'
#!/usr/bin/env python3
"""
Aplica as modificacoes do DLSS 5 em overlay.py e live.py.
Idempotente: rodar duas vezes nao duplica nada.
"""
import sys
from pathlib import Path

if not Path("overlay.py").exists() or not Path("live.py").exists():
    sys.exit("ERRO: rode este script na pasta que contem overlay.py e live.py")
if not Path("dlss5.py").exists():
    sys.exit("ERRO: dlss5.py nao encontrado (adicione-o antes de rodar este patch)")

# ==================== OVERLAY.PY ====================
src = Path("overlay.py").read_text()
original = src

if "from dlss5 import DLSS5Pipeline" in src:
    print("overlay.py: ja patcheado, pulando.")
else:
    # 1. Import + PASSTHROUGH_FRAG
    OLD = "from framegen import GenRef\n"
    NEW = '''from framegen import GenRef
from dlss5 import DLSS5Pipeline

PASSTHROUGH_FRAG = """#version 330 core
in vec2 uv;
out vec4 fragColor;
uniform sampler2D tex;
void main() { fragColor = texture(tex, uv); }
"""
'''
    assert OLD in src, "overlay.py: import 'from framegen import GenRef' nao encontrado"
    src = src.replace(OLD, NEW, 1)

    # 2. GLView.__init__
    OLD = '''    def __init__(self, feed, rcas, gpu_flag, fsr_mode="fsr1"):
        super().__init__()
        self.feed, self.rcas = feed, rcas
        self.gpu_flag = gpu_flag
        self.fsr_mode = fsr_mode
        self.gl_ok = True
        self.interp_ok = False
        self.temporal_ok = fsr_mode in ("fsr2", "fsr3", "dlss1", "dlss3")
        self.dlss_ok = fsr_mode == "dlss1"
        self.dlss3_ok = fsr_mode == "dlss3"
'''
    NEW = '''    def __init__(self, feed, rcas, gpu_flag, fsr_mode="fsr1", dlss5_strength=0.6):
        super().__init__()
        self.feed, self.rcas = feed, rcas
        self.gpu_flag = gpu_flag
        self.fsr_mode = fsr_mode
        self.dlss5_strength = dlss5_strength
        self.gl_ok = True
        self.interp_ok = False
        self.temporal_ok = fsr_mode in ("fsr2", "fsr3", "dlss1", "dlss3", "dlss5")
        self.dlss_ok = fsr_mode == "dlss1"
        self.dlss3_ok = fsr_mode == "dlss3"
        self.dlss5_ok = fsr_mode == "dlss5"
        self.dlss5 = None
        self.dlss5_params = {
            "strength": max(0.0, min(1.0, dlss5_strength)),
            "ao": 0.7, "indirect": 0.6, "contrast": 0.4, "material": 0.8, "sr": 0.7,
        }
        self.p_passthrough = None
'''
    assert OLD in src, "overlay.py: GLView.__init__ nao encontrado"
    src = src.replace(OLD, NEW, 1)

    # 3. initializeGL: passthrough + DLSS5 init
    OLD = '''        self._init_dlss(GL)
        self._init_dlss3(GL)

    def _init_dlss(self, GL):'''
    NEW = '''        try:
            self.p_passthrough = self._compile(PASSTHROUGH_FRAG, VERT_FBO)
        except Exception:
            self.p_passthrough = None
        self._init_dlss(GL)
        self._init_dlss3(GL)
        self._init_dlss5(GL)

    def _init_dlss5(self, GL):
        if not self.dlss5_ok:
            return
        try:
            self.dlss5 = DLSS5Pipeline()
            compile_fbo = lambda f: self._compile(f, VERT_FBO)
            self.dlss5.init_gl(GL, self.vao, self.defaultFramebufferObject(), compile_fbo)
            _log("DLSS 5: pipeline inicializado (lighting + material + SR)")
        except Exception as e:
            _log(f"DLSS 5 indisponivel: {e}; usando FSR 1")
            self.dlss5_ok = False
            self.dlss5 = None

    def _init_dlss(self, GL):'''
    assert OLD in src, "overlay.py: initializeGL/fim da chain nao encontrado"
    src = src.replace(OLD, NEW, 1)

    # 4. _interp_texs (novo metodo depois de _interp)
    OLD = '''        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        GL.glActiveTexture(GL.GL_TEXTURE0)
        return self.fbo_tex

    def _temporal_accumulate(self, frame, flow):'''
    NEW = '''        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        GL.glActiveTexture(GL.GL_TEXTURE0)
        return self.fbo_tex

    def _interp_texs(self, texA, texB, flowF, flowB, t, w, h):
        GL = self.GL
        self._ensure_fbo(w, h)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo)
        GL.glViewport(0, 0, w, h)
        GL.glUseProgram(self.iprog)
        for unit, (tex, name) in enumerate(zip(
                (texA, texB, flowF, flowB), ("texA", "texB", "flowF", "flowB"))):
            GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glUniform1i(self.iloc[name], unit)
        GL.glUniform2f(self.iloc["size"], float(w), float(h))
        GL.glUniform1f(self.iloc["t"], float(t))
        GL.glBindVertexArray(self.vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        GL.glActiveTexture(GL.GL_TEXTURE0)
        return self.fbo_tex

    def _temporal_accumulate(self, frame, flow):'''
    assert OLD in src, "overlay.py: fim de _interp nao encontrado"
    src = src.replace(OLD, NEW, 1)

    # 5. _temporal_accumulate_texs (novo metodo depois de _temporal_accumulate)
    OLD = '''        self.hist_tex_a, self.hist_tex_b = self.hist_tex_b, self.hist_tex_a
        return self.hist_tex_a

    def paintGL(self):'''
    NEW = '''        self.hist_tex_a, self.hist_tex_b = self.hist_tex_b, self.hist_tex_a
        return self.hist_tex_a

    def _temporal_accumulate_texs(self, cur_tex, flow_tex, w, h):
        GL = self.GL
        if self.hist_tex_a is None or self.hist_size != (w, h):
            if self.hist_tex_a is not None:
                GL.glDeleteTextures([self.hist_tex_a, self.hist_tex_b])
            self.hist_tex_a = self._make_rgb8_tex(w, h)
            self.hist_tex_b = self._make_rgb8_tex(w, h)
            self.hist_size = (w, h)
            self.hist_initialized = False
        if not self.hist_initialized:
            if self.p_passthrough is not None:
                GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.hist_fbo)
                GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                           GL.GL_TEXTURE_2D, self.hist_tex_a, 0)
                GL.glViewport(0, 0, w, h)
                GL.glUseProgram(self.p_passthrough)
                GL.glActiveTexture(GL.GL_TEXTURE0)
                GL.glBindTexture(GL.GL_TEXTURE_2D, cur_tex)
                GL.glUniform1i(GL.glGetUniformLocation(self.p_passthrough, "tex"), 0)
                GL.glBindVertexArray(self.vao)
                GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
                GL.glBindVertexArray(0)
                GL.glUseProgram(0)
                GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
                GL.glActiveTexture(GL.GL_TEXTURE0)
            self.hist_initialized = True
            return self.hist_tex_a
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.hist_fbo)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                   GL.GL_TEXTURE_2D, self.hist_tex_b, 0)
        GL.glViewport(0, 0, w, h)
        GL.glUseProgram(self.tprog)
        for unit, (tex, name) in enumerate(zip(
                (self.hist_tex_a, cur_tex, flow_tex),
                ("texHistory", "texCurrent", "texFlow"))):
            GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glUniform1i(self.tloc[name], unit)
        GL.glUniform2f(self.tloc["size"], float(w), float(h))
        GL.glUniform1f(self.tloc["alpha"], 0.20)
        GL.glBindVertexArray(self.vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        GL.glActiveTexture(GL.GL_TEXTURE0)
        self.hist_tex_a, self.hist_tex_b = self.hist_tex_b, self.hist_tex_a
        return self.hist_tex_a

    def paintGL(self):'''
    assert OLD in src, "overlay.py: fim de _temporal_accumulate nao encontrado"
    src = src.replace(OLD, NEW, 1)

    # 6. _draw_gl inteiro
    OLD = '''    def _draw_gl(self, feed, W, H):
        GL = self.GL
        frame, on, flow = feed.cur

        if isinstance(frame, GenRef):
            h, w = frame.shape[:2]
            tex = None
            if self.interp_ok:
                try:
                    tex = self._interp(frame)
                except Exception:
                    self.interp_ok = False
                    self.gpu_flag[0] = False
                    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
                    GL.glUseProgram(0)
            if tex is None:
                frame = frame.render()
                h, w = frame.shape[:2]
            flow = None
        elif self.temporal_ok and flow is not None:
            h, w = frame.shape[:2]
            tex = self._temporal_accumulate(frame, flow)
        else:
            h, w = frame.shape[:2]
            tex = None

        if tex is None:
            tex = self._frame_tex(frame)
            h, w = frame.shape[:2]

        if not self._logged_first:
            self._logged_first = True
            try:
                mean_str = (f"{float(frame.mean()):.1f}"
                            if isinstance(frame, np.ndarray) else "?")
                x_, y_, vw_, vh_ = _fit(w, h, W, H)
                _log(f"primeiro quadro {w}x{h} -> viewport {vw_}x{vh_} "
                     f"framebuffer {W}x{H} fit={FIT_MODE} brilho medio={mean_str}")
            except Exception:
                pass

        GL.glViewport(0, 0, W, H)
        GL.glClearColor(0, 0, 0, 1)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        x, y, vw, vh = _fit(w, h, W, H)
        GL.glViewport(x, y, vw, vh)

        is_gen = isinstance(frame, GenRef)
        use_dlss3 = self.dlss3_ok and not is_gen
        use_dlss1 = self.dlss_ok and not is_gen and not use_dlss3

        if use_dlss3:
            GL.glUseProgram(self.d3prog)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glUniform1i(self.d3loc["tex"], 0)
            GL.glActiveTexture(GL.GL_TEXTURE1)
            flow_tex = self._flow_tex(flow) if flow is not None else self.zero_flow
            GL.glBindTexture(GL.GL_TEXTURE_2D, flow_tex)
            GL.glUniform1i(self.d3loc["texFlow"], 1)
            GL.glUniform2f(self.d3loc["srcSize"], float(w), float(h))
            GL.glUniform2f(self.d3loc["dstSize"], float(vw), float(vh))
        elif use_dlss1:
            GL.glUseProgram(self.dprog)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glUniform1i(self.dloc["tex"], 0)
            GL.glActiveTexture(GL.GL_TEXTURE1)
            flow_tex = self._flow_tex(flow) if flow is not None else self.zero_flow
            GL.glBindTexture(GL.GL_TEXTURE_2D, flow_tex)
            GL.glUniform1i(self.dloc["texFlow"], 1)
            GL.glUniform2f(self.dloc["srcSize"], float(w), float(h))
            GL.glUniform2f(self.dloc["dstSize"], float(vw), float(vh))
            GL.glUniform1f(self.dloc["sharpness"], 0.4)
            GL.glUniform1f(self.dloc["saturation"], 1.1)
            GL.glUniform1f(self.dloc["blur_strength"], 0.5)
        else:
            GL.glUseProgram(self.prog)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glUniform1i(self.loc["tex"], 0)
            GL.glUniform2f(self.loc["srcSize"], float(w), float(h))
            GL.glUniform2f(self.loc["dstSize"], float(vw), float(vh))
            GL.glUniform1f(self.loc["rcas"], self.rcas)

        GL.glBindVertexArray(self.vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)
'''
    NEW = '''    def _draw_gl(self, feed, W, H):
        GL = self.GL
        frame, on, flow = feed.cur

        if isinstance(frame, GenRef):
            h, w = frame.cur.shape[:2]
            tex = None
            if self.dlss5_ok and self.dlss5 is not None and self.interp_ok:
                try:
                    texA = self.dlss5.get_processed(
                        frame.prev, self._frame_tex(frame.prev),
                        self._flow_tex(frame.fw), w, h, self.dlss5_params)
                    texB = self.dlss5.get_processed(
                        frame.cur, self._frame_tex(frame.cur),
                        self._flow_tex(frame.bw), w, h, self.dlss5_params)
                    tex = self._interp_texs(
                        texA, texB,
                        self._flow_tex(frame.fw), self._flow_tex(frame.bw),
                        frame.t, w, h)
                except Exception as e:
                    _log(f"DLSS 5 interp falhou: {e}")
                    tex = None
            if tex is None and self.interp_ok:
                try:
                    tex = self._interp(frame)
                except Exception:
                    self.interp_ok = False
                    self.gpu_flag[0] = False
                    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
                    GL.glUseProgram(0)
            if tex is None:
                frame = frame.render()
                h, w = frame.shape[:2]
            flow = None
        elif self.dlss5_ok and self.dlss5 is not None:
            h, w = frame.shape[:2]
            try:
                enhanced = self.dlss5.get_processed(
                    frame, self._frame_tex(frame),
                    self._flow_tex(flow) if flow is not None else None,
                    w, h, self.dlss5_params)
                if self.temporal_ok and flow is not None:
                    tex = self._temporal_accumulate_texs(
                        enhanced, self._flow_tex(flow), w, h)
                else:
                    tex = enhanced
            except Exception as e:
                _log(f"DLSS 5 falhou: {e}; caindo para FSR 1")
                self.dlss5_ok = False
                tex = (self._temporal_accumulate(frame, flow)
                       if (self.temporal_ok and flow is not None) else None)
            flow = None
        elif self.temporal_ok and flow is not None:
            h, w = frame.shape[:2]
            tex = self._temporal_accumulate(frame, flow)
        else:
            h, w = frame.shape[:2]
            tex = None

        if tex is None:
            tex = self._frame_tex(frame)
            h, w = frame.shape[:2]

        if not self._logged_first:
            self._logged_first = True
            try:
                mean_str = (f"{float(frame.mean()):.1f}"
                            if isinstance(frame, np.ndarray) else "?")
                x_, y_, vw_, vh_ = _fit(w, h, W, H)
                _log(f"primeiro quadro {w}x{h} -> viewport {vw_}x{vh_} "
                     f"framebuffer {W}x{H} fit={FIT_MODE} brilho medio={mean_str}")
            except Exception:
                pass

        GL.glViewport(0, 0, W, H)
        GL.glClearColor(0, 0, 0, 1)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        x, y, vw, vh = _fit(w, h, W, H)
        GL.glViewport(x, y, vw, vh)

        is_gen = isinstance(frame, GenRef)
        use_dlss3 = self.dlss3_ok and not is_gen and not self.dlss5_ok
        use_dlss1 = self.dlss_ok and not is_gen and not use_dlss3 and not self.dlss5_ok
        use_dlss5 = self.dlss5_ok and not is_gen

        if use_dlss5:
            # DLSS5 ja processou: so upscale EASU (rcas=0 para nao double-sharpen)
            GL.glUseProgram(self.prog)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glUniform1i(self.loc["tex"], 0)
            GL.glUniform2f(self.loc["srcSize"], float(w), float(h))
            GL.glUniform2f(self.loc["dstSize"], float(vw), float(vh))
            GL.glUniform1f(self.loc["rcas"], 0.0)
        elif use_dlss3:
            GL.glUseProgram(self.d3prog)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glUniform1i(self.d3loc["tex"], 0)
            GL.glActiveTexture(GL.GL_TEXTURE1)
            flow_tex = self._flow_tex(flow) if flow is not None else self.zero_flow
            GL.glBindTexture(GL.GL_TEXTURE_2D, flow_tex)
            GL.glUniform1i(self.d3loc["texFlow"], 1)
            GL.glUniform2f(self.d3loc["srcSize"], float(w), float(h))
            GL.glUniform2f(self.d3loc["dstSize"], float(vw), float(vh))
        elif use_dlss1:
            GL.glUseProgram(self.dprog)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glUniform1i(self.dloc["tex"], 0)
            GL.glActiveTexture(GL.GL_TEXTURE1)
            flow_tex = self._flow_tex(flow) if flow is not None else self.zero_flow
            GL.glBindTexture(GL.GL_TEXTURE_2D, flow_tex)
            GL.glUniform1i(self.dloc["texFlow"], 1)
            GL.glUniform2f(self.dloc["srcSize"], float(w), float(h))
            GL.glUniform2f(self.dloc["dstSize"], float(vw), float(vh))
            GL.glUniform1f(self.dloc["sharpness"], 0.4)
            GL.glUniform1f(self.dloc["saturation"], 1.1)
            GL.glUniform1f(self.dloc["blur_strength"], 0.5)
        else:
            GL.glUseProgram(self.prog)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glUniform1i(self.loc["tex"], 0)
            GL.glUniform2f(self.loc["srcSize"], float(w), float(h))
            GL.glUniform2f(self.loc["dstSize"], float(vw), float(vh))
            GL.glUniform1f(self.loc["rcas"], self.rcas)

        GL.glBindVertexArray(self.vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)
'''
    assert OLD in src, "overlay.py: _draw_gl nao encontrado"
    src = src.replace(OLD, NEW, 1)

    # 7. run_overlay signature
    OLD = '''def run_overlay(q, size, st, present, hud_lines, hud_on, on_fg, *, output=0, geometry=None,
                soft=False, managed=False, mask=False, sharpness=0.2, seconds=0, fit="auto",
                gpu_interp=None, tracker=None, follow_size=False, fsr_mode="fsr1"):'''
    NEW = '''def run_overlay(q, size, st, present, hud_lines, hud_on, on_fg, *, output=0, geometry=None,
                soft=False, managed=False, mask=False, sharpness=0.2, seconds=0, fit="auto",
                gpu_interp=None, tracker=None, follow_size=False, fsr_mode="fsr1",
                dlss5_strength=0.6):'''
    assert OLD in src, "overlay.py: assinatura run_overlay nao encontrada"
    src = src.replace(OLD, NEW, 1)

    # 8. run_overlay GLView constructor
    OLD = '''    view = GLView(feed, 0.0 if sharpness < 0 else 2.0 ** -sharpness, gpu, fsr_mode=fsr_mode)'''
    NEW = '''    view = GLView(feed, 0.0 if sharpness < 0 else 2.0 ** -sharpness, gpu,
                  fsr_mode=fsr_mode, dlss5_strength=dlss5_strength)'''
    assert OLD in src, "overlay.py: criacao do GLView nao encontrada"
    src = src.replace(OLD, NEW, 1)

    Path("overlay.py").write_text(src)
    print(f"overlay.py: patch aplicado ({len(src) - len(original):+d} bytes)")

# ==================== LIVE.PY ====================
src = Path("live.py").read_text()
original = src

if '"dlss5"' in src and "--dlss5-strength" in src:
    print("live.py: ja patcheado, pulando.")
else:
    # 1. needs_temporal
    OLD = '    needs_temporal = args.fsr_mode in ("fsr2", "fsr3", "dlss1", "dlss3")'
    NEW = '    needs_temporal = args.fsr_mode in ("fsr2", "fsr3", "dlss1", "dlss3", "dlss5")'
    assert OLD in src, "live.py: needs_temporal nao encontrado"
    src = src.replace(OLD, NEW, 1)

    # 2. --fsr-mode choices
    OLD = '''    p.add_argument("--fsr-mode", choices=("fsr1", "fsr2", "fsr3", "fsr4", "dlss1", "dlss3"), default="fsr1",'''
    NEW = '''    p.add_argument("--fsr-mode", choices=("fsr1", "fsr2", "fsr3", "fsr4", "dlss1", "dlss3", "dlss5"), default="fsr1",'''
    assert OLD in src, "live.py: choices do --fsr-mode nao encontrado"
    src = src.replace(OLD, NEW, 1)

    # 3. --dlss5-strength (depois de --scale-factor)
    OLD = '''    p.add_argument("--scale-factor", type=int, default=1, help="ver --fsr-mode fsr4")'''
    NEW = '''    p.add_argument("--scale-factor", type=int, default=1, help="ver --fsr-mode fsr4")
    p.add_argument("--dlss5-strength", type=float, default=0.6, metavar="0..1",
                   help="DLSS 5: forca dos efeitos (iluminacao+materiais+SR). 0=off, 1=maximo.")'''
    assert OLD in src, "live.py: --scale-factor nao encontrado"
    src = src.replace(OLD, NEW, 1)

    # 4. chamada run_overlay
    OLD = '''                        follow_size=follow_size, fsr_mode=args.fsr_mode)'''
    NEW = '''                        follow_size=follow_size, fsr_mode=args.fsr_mode,
                        dlss5_strength=args.dlss5_strength)'''
    assert OLD in src, "live.py: chamada run_overlay nao encontrada"
    src = src.replace(OLD, NEW, 1)

    Path("live.py").write_text(src)
    print(f"live.py: patch aplicado ({len(src) - len(original):+d} bytes)")

print("\nPronto. Valide com:")
print('  python -c "import ast; [ast.parse(open(f).read()) for f in (\'overlay.py\',\'live.py\',\'dlss5.py\',\'framegen_gui.py\')]; print(\'OK\')"')
PYEOF