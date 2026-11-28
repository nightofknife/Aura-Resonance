#include <algorithm>
#include <cstdint>
#include <immintrin.h>
#include <new>
#include <vector>

namespace {
constexpr int metric_count = 5;
constexpr int boss_actions = 16;
constexpr int block_rows = 512;
constexpr double tolerance = 1e-12;

struct Work {
    int lane;
    int begin;
    int end;
};

int lane_for(int state, const std::int32_t* offsets, int lanes) {
    int lane = 0;
    while (lane + 1 < lanes && state >= offsets[lane + 1]) ++lane;
    return lane;
}

template<bool zero>
void boss_value(
    const double* previous, const std::int32_t* edges, double weight, double held, double* output
) {
    const auto chance = _mm256_set1_pd(weight);
    const auto terminal = _mm256_mul_pd(_mm256_setr_pd(1., held, 0., 0.), chance);
    const auto gain = _mm256_setr_pd(0., 0., 0., weight);
    auto sum = _mm256_setzero_pd();
    double moment = 0.;
    for (int action = 0; action < boss_actions; ++action) {
        const int edge = edges[action];
        if (edge >= 0) {
            if constexpr (zero) {
                if (edge & 1) sum = _mm256_add_pd(sum, gain);
            } else {
                const double* next = previous + metric_count * (edge >> 1);
                auto values = _mm256_loadu_pd(next);
                if (edge & 1) values = _mm256_add_pd(values, _mm256_setr_pd(0., 0., 0., 1.));
                sum = _mm256_add_pd(sum, _mm256_mul_pd(values, chance));
                moment += (next[4] + next[0]) * weight;
            }
        } else if (edge == -2) {
            sum = _mm256_add_pd(sum, terminal);
            moment += weight;
        }
    }
    _mm256_storeu_pd(output, sum);
    output[4] = moment;
}

template<bool encounter_first>
bool better(const double* option, int gain, const double* best, int best_gain) {
    constexpr int primary = encounter_first ? 0 : 1;
    constexpr int secondary = encounter_first ? 1 : 0;
    double diff = option[primary] - best[primary];
    if (diff > tolerance) return true;
    if (diff < -tolerance) return false;
    diff = option[secondary] - best[secondary];
    if (diff > tolerance) return true;
    if (diff < -tolerance) return false;
    diff = (option[2] + gain) - (best[2] + best_gain);
    if (diff > tolerance) return true;
    if (diff < -tolerance) return false;
    return best[4] - option[4] > tolerance;
}

template<bool encounter_first>
int choose(
    const Work& work, const std::int32_t* own, int actions, const std::int32_t* offsets,
    const double* boss_values, double* output, double held
) {
    const double terminal[metric_count] = {1., held, 0., 0., 1.};
    const double* no_gain = boss_values + metric_count * offsets[work.lane];
    const int gain_offset = offsets[work.lane + 1];
    const double* with_gain = gain_offset < 0 ? no_gain : boss_values + metric_count * gain_offset;
    int invalid = 0;
    for (int row = work.begin; row < work.end; ++row) {
        const auto* edges = own + row * actions;
        const double* best = nullptr;
        int best_gain = 0;
        for (int action = 0; action < actions; ++action) {
            const int edge = edges[action];
            if (edge == -1) continue;
            const int gain = edge >= 0 && (edge & 1);
            const double* next = edge == -2 ? terminal :
                (gain ? with_gain : no_gain) + metric_count * (edge >> 1);
            if (!best || better<encounter_first>(next, gain, best, best_gain)) {
                best = next;
                best_gain = gain;
            }
        }
        if (best) {
            double* result = output + metric_count * row;
            _mm256_storeu_pd(result, _mm256_loadu_pd(best));
            result[2] += best_gain;
            result[4] = best[4];
        } else {
            invalid = 1;
        }
    }
    return invalid;
}
}

extern "C" __declspec(dllexport) int aura_deep_dive_solver_abi() {
    return 3;
}

extern "C" __declspec(dllexport) int step_packed(
    const double* previous, const std::int32_t* own, const std::int32_t* boss,
    const double* boss_weight, const std::int32_t* limits, const std::int32_t* padded_offsets,
    int actions, int lanes, int baseline, int encounter_first, int workers, int zero,
    double* boss_values, double* current
) try {
    std::vector<Work> blocks;
    blocks.reserve(lanes * ((limits[0] + block_rows - 1) / block_rows));
    for (int lane = 0; lane < lanes; ++lane) {
        for (int begin = 0; begin < limits[lane]; begin += block_rows) {
            blocks.push_back({lane, begin, std::min(begin + block_rows, int(limits[lane]))});
        }
    }
    const int block_count = int(blocks.size());
    int invalid = 0;
    #pragma omp parallel num_threads(workers) if(workers > 1) reduction(|:invalid)
    {
        #pragma omp for schedule(static)
        for (int index = 0; index < block_count; ++index) {
            const Work block = blocks[index];
            const int start = padded_offsets[block.lane];
            const double* layer = previous + metric_count * start;
            for (int row = block.begin; row < block.end; ++row) {
                const auto* edges = boss + boss_actions * row;
                double* output = boss_values + metric_count * (start + row);
                if (zero) boss_value<true>(layer, edges, boss_weight[row], baseline + block.lane, output);
                else boss_value<false>(layer, edges, boss_weight[row], baseline + block.lane, output);
            }
        }
        #pragma omp for schedule(static)
        for (int index = 0; index < block_count; ++index) {
            const Work block = blocks[index];
            double* output = current + metric_count * padded_offsets[block.lane];
            if (encounter_first) {
                invalid |= choose<true>(block, own, actions, padded_offsets, boss_values, output,
                                        baseline + block.lane);
            } else {
                invalid |= choose<false>(block, own, actions, padded_offsets, boss_values, output,
                                         baseline + block.lane);
            }
        }
    }
    return invalid;
} catch (const std::bad_alloc&) {
    return -1;
}

extern "C" __declspec(dllexport) void live_packed(
    const double* previous, const std::int32_t* boss, const double* weight,
    const std::int32_t* rows, const std::int32_t* offsets, const std::int32_t* counts,
    int options, int baseline, double* output
) {
    for (int i = 0; i < options; ++i) {
        const int row = rows[i];
        const int lane = counts[i];
        boss_value<false>(previous + metric_count * offsets[lane], boss + boss_actions * row,
                          weight[row], baseline + lane, output + metric_count * i);
    }
}

namespace {
template<bool own>
std::vector<int> dependencies(
    const std::int32_t* source, int count, const std::int32_t* edges, int columns,
    const std::int32_t* offsets, int lanes, std::uint8_t* present
) {
    std::vector<int> result;
    result.reserve(count * 2);
    for (int i = 0; i < count; ++i) {
        const int state = source[i];
        const int lane = lane_for(state, offsets, lanes);
        const int row = state - offsets[lane];
        const auto* targets = edges + row * columns;
        for (int action = 0; action < columns; ++action) {
            const int edge = targets[action];
            if (edge < 0) continue;
            const int next_lane = own ? lane + (edge & 1) : lane;
            const int target = offsets[next_lane] + (edge >> 1);
            if (!present[target]) {
                present[target] = 1;
                result.push_back(target);
            }
        }
    }
    return result;
}

void requested_boss(
    int state, const double* previous, const std::int32_t* boss, const double* weights,
    const std::int32_t* offsets, int lanes, int baseline, double* output
) {
    const int lane = lane_for(state, offsets, lanes);
    const int row = state - offsets[lane];
    boss_value<false>(previous + metric_count * offsets[lane], boss + boss_actions * row,
                      weights[row], baseline + lane, output + metric_count * state);
}

template<bool encounter_first>
int requested_player(
    int state, const double* boss_values, const std::int32_t* own, int actions,
    const std::int32_t* offsets, int lanes, int baseline, double* output
) {
    const int lane = lane_for(state, offsets, lanes);
    const int row = state - offsets[lane];
    const auto* targets = own + row * actions;
    const double terminal[metric_count] = {1., double(baseline + lane), 0., 0., 1.};
    const double* best = nullptr;
    int best_gain = 0;
    for (int action = 0; action < actions; ++action) {
        const int edge = targets[action];
        if (edge == -1) continue;
        const int gain = edge >= 0 && (edge & 1);
        const double* candidate = edge == -2 ? terminal :
            boss_values + metric_count * (offsets[lane + gain] + (edge >> 1));
        if (!best || better<encounter_first>(candidate, gain, best, best_gain)) {
            best = candidate;
            best_gain = gain;
        }
    }
    if (!best) return 1;
    double* dest = output + metric_count * state;
    std::copy_n(best, metric_count, dest);
    dest[2] += best_gain;
    return 0;
}
}

extern "C" __declspec(dllexport) int step_two_requested(
    const double* previous, const std::int32_t* own, const std::int32_t* boss, const double* weights,
    const std::int32_t* offsets, const std::int32_t* requested,
    int actions, int lanes, int baseline, int encounter_first, int request_count, int workers,
    double* first_boss, double* first_player, double* second_boss,
    std::uint8_t* first_boss_present, std::uint8_t* first_player_present, std::uint8_t* second_boss_present,
    double* output
) try {
    const auto need_second_boss = dependencies<true>(requested, request_count, own, actions, offsets,
                                                     lanes, second_boss_present);
    const auto need_first_player = dependencies<false>(need_second_boss.data(), int(need_second_boss.size()),
                                                        boss, boss_actions, offsets, lanes, first_player_present);
    const auto need_first_boss = dependencies<true>(need_first_player.data(), int(need_first_player.size()),
                                                    own, actions, offsets, lanes, first_boss_present);
    const int first_boss_count = int(need_first_boss.size());
    const int first_player_count = int(need_first_player.size());
    const int second_boss_count = int(need_second_boss.size());
    const auto evaluate_player = encounter_first ? requested_player<true> : requested_player<false>;
    int invalid = 0;
    #pragma omp parallel num_threads(workers) if(first_boss_count >= 64) reduction(|:invalid)
    {
        #pragma omp for schedule(static)
        for (int i = 0; i < first_boss_count; ++i) {
            requested_boss(need_first_boss[i], previous, boss, weights, offsets, lanes, baseline, first_boss);
        }
        #pragma omp for schedule(static)
        for (int i = 0; i < first_player_count; ++i) {
            invalid |= evaluate_player(need_first_player[i], first_boss, own, actions, offsets,
                                       lanes, baseline, first_player);
        }
        #pragma omp for schedule(static)
        for (int i = 0; i < second_boss_count; ++i) {
            requested_boss(need_second_boss[i], first_player, boss, weights, offsets, lanes, baseline, second_boss);
        }
        #pragma omp for schedule(static)
        for (int i = 0; i < request_count; ++i) {
            invalid |= evaluate_player(requested[i], second_boss, own, actions, offsets, lanes, baseline, output);
        }
    }
    return invalid;
} catch (const std::bad_alloc&) {
    return -1;
}
