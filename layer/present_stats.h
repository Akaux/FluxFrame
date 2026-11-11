// Estatística de present por janela de 1 s (fps, 1% low, frametime). Sem dependência do Vulkan:
// dá para testar com g++ puro. Mesmas definições do bench.py do FluxFrame.
#pragma once
#include <algorithm>
#include <cstddef>
#include <vector>

struct FpsRow { double t, fps, low1, avg_ms, max_ms; };

class PresentStats {
public:
    // Chame a cada present com o tempo em segundos. Devolve true (e preenche `row`) ao fechar 1 s.
    bool on_present(double now, FpsRow* row) {
        if (!started_) { started_ = true; t0_ = last_ = win_ = now; return false; }
        dts_.push_back(now - last_);
        last_ = now;
        if (now - win_ < 1.0) return false;
        std::vector<double> s = dts_;
        std::sort(s.begin(), s.end());
        const size_t n = s.size();
        double sum = 0;
        for (double d : s) sum += d;
        const double p99 = s[static_cast<size_t>(0.99 * (n - 1))];   // 99% dos intervalos são menores
        row->t = now - t0_;
        row->fps = n / (now - win_);
        row->low1 = p99 > 0 ? 1.0 / p99 : 0.0;
        row->avg_ms = sum / n * 1000.0;
        row->max_ms = s.back() * 1000.0;
        dts_.clear();
        win_ = now;
        return true;
    }
private:
    bool started_ = false;
    double t0_ = 0, last_ = 0, win_ = 0;
    std::vector<double> dts_;
};
