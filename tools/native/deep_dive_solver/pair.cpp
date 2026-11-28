#include <algorithm>
#include <cstdint>
#include <new>
#include <vector>

namespace {
using Score = std::int64_t;
struct Value {
    Score probability;
    Score moment;
};

bool better(Value candidate, Value current) {
    return candidate.probability > current.probability ||
           (candidate.probability == current.probability && candidate.moment < current.moment);
}
}

extern "C" __declspec(dllexport) Score pair_deadline(
    const std::int32_t* own_next, const std::int16_t* pickup_slots, const std::int32_t* boss_next,
    int rows, int columns, int slots, int horizon,
    Score* probability, Score* moment, Score* tile_p, Score* tile_m
) try {
    // Every denominator is a power of 48. Choose a width that also proves
    // the largest success-time moment fits; Python remains exact beyond it.
    if (horizon < 1 || horizon > 10) return 0;
    std::vector<Value> previous(rows, {0, 0});
    std::vector<Value> current(rows);
    std::vector<Value> boss(rows);
    std::vector<int> factors(rows, 0);
    for (int row = 0; row < rows; ++row) {
        int legal = 0;
        for (int action = 0; action < 16; ++action) {
            legal += boss_next[action * rows + row] != -1;
        }
        if (!legal || 48 % legal) return 0;
        factors[row] = 48 / legal;
    }
    Score unit = 1;
    const int width = slots + 1;
    std::fill_n(tile_p, rows * width, -1);
    std::fill_n(tile_m, rows * width, 0);
    for (int depth = 1; depth <= horizon; ++depth) {
        for (int row = 0; row < rows; ++row) {
            Value value{0, 0};
            for (int action = 0; action < 16; ++action) {
                const int target = boss_next[action * rows + row];
                if (target == -2) {
                    value.probability += unit;
                    value.moment += unit;
                } else if (target >= 0) {
                    value.probability += previous[target].probability;
                    value.moment += previous[target].moment + previous[target].probability;
                }
            }
            boss[row] = {value.probability * factors[row], value.moment * factors[row]};
        }
        unit *= 48;
        for (int row = 0; row < rows; ++row) {
            Value best{-1, 0};
            for (int action = 0; action < columns; ++action) {
                const int index = action * rows + row;
                const int target = own_next[index];
                if (target == -1) continue;
                const Value candidate = target == -2 ? Value{unit, unit} : boss[target];
                if (better(candidate, best)) best = candidate;
                if (depth == horizon) {
                    const int tile = row * width + pickup_slots[index];
                    if (better(candidate, {tile_p[tile], tile_m[tile]})) {
                        tile_p[tile] = candidate.probability;
                        tile_m[tile] = candidate.moment;
                    }
                }
            }
            current[row] = best;
        }
        previous.swap(current);
    }
    for (int row = 0; row < rows; ++row) {
        probability[row] = previous[row].probability;
        moment[row] = previous[row].moment;
    }
    return unit;
} catch (const std::bad_alloc&) {
    return -1;
}
