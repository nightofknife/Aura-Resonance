#include <algorithm>
#include <cstdint>
#include <new>
#include <vector>

namespace {
struct Rule {
    int state_base;
    std::int16_t map_index;
    std::int16_t pickup;
};

}

extern "C" __declspec(dllexport) int build_packed_transitions(
    const std::int32_t* players, const std::int32_t* bosses, const std::int32_t* first,
    const std::int32_t* second, const std::int32_t* representatives,
    const std::int32_t* moves, const std::int32_t* boss_moves,
    const std::int32_t* actor_rotations, const std::int32_t* rotations, const std::int32_t* inverse,
    const std::int32_t* symmetries, const std::int32_t* frames, const std::int32_t* orbits,
    const std::int32_t* placement_ids, const std::int32_t* state_ids, const std::uint8_t* canonical_maps,
    int rows, int representative_count, int size, int columns, int placements, int rotation_count, int workers,
    std::int32_t* own_next, std::int32_t* boss_next, double* boss_weight
) try {
    const int slots = 6 * size * size;
    const int slot_stride = slots + 1;
    const int own_actions = 8 * columns;
    const int action_count = own_actions + 16;
    const int pair_count = representative_count * slots;
    const Rule illegal{-1, 0, std::int16_t(slots)};
    std::vector<Rule> rules(pair_count * action_count, illegal);
    std::vector<double> weights(pair_count, 0.);
    auto regular = [&](int next_p, int next_b, int rotation, int pickup) -> Rule {
        const int frame = frames[next_p];
        const int canonical_boss = symmetries[frame * slot_stride + next_b];
        return {(orbits[next_p] * slots + canonical_boss) * placements,
                std::int16_t(frame * rotation_count + rotation), std::int16_t(pickup)};
    };

    // Movement legality, rotations and canonical role positions depend only
    // on the player/boss pair, and are reused across all inspiration placements.
    for (int pair = 0; pair < pair_count; ++pair) {
        const int p = representatives[pair / slots];
        const int b = pair % slots;
        if (p == b) continue;
        Rule* rule = rules.data() + pair * action_count;
        for (int move = 0; move < columns; ++move) {
            const int destination = moves[p * columns + move];
            if (destination < 0) continue;
            for (int turn = 0; turn < 4; ++turn) {
                const int rotation = actor_rotations[destination * 4 + turn];
                const auto* rot = rotations + rotation * slot_stride;
                rule[move * 4 + turn] = destination == b
                    ? Rule{-2, 0, std::int16_t(destination)}
                    : regular(rot[destination], rot[b], rotation, destination);
            }
        }
        for (int turn = 0; turn < 4; ++turn) {
            const int rotation = actor_rotations[p * 4 + turn];
            const auto* rot = rotations + rotation * slot_stride;
            for (int move = 0; move < columns; ++move) {
                const int destination = moves[rot[p] * columns + move];
                if (destination < 0) continue;
                const int pickup = inverse[rotation * slot_stride + destination];
                rule[4 * columns + turn * columns + move] = destination == rot[b]
                    ? Rule{-2, 0, std::int16_t(pickup)}
                    : regular(destination, rot[b], rotation, pickup);
            }
        }
        const auto* neighbours = boss_moves + b * 4;
        const bool player_adjacent = std::find(neighbours, neighbours + 4, p) != neighbours + 4;
        int allowed = 0;
        for (int move = 0; move < 4; ++move) {
            const int destination = neighbours[move];
            if (destination < 0 || (player_adjacent && destination != p)) {
                continue;
            }
            ++allowed;
            for (int turn = 0; turn < 4; ++turn) {
                const int rotation = actor_rotations[destination * 4 + turn];
                const auto* rot = rotations + rotation * slot_stride;
                rule[own_actions + move * 4 + turn] = destination == p
                    ? Rule{-2, 0, std::int16_t(destination)}
                    : regular(rot[p], rot[destination], rotation, destination);
            }
        }
        if (!allowed) return 1;
        weights[pair] = 1. / (allowed * 4);
    }

    int invalid = 0;
    #pragma omp parallel for num_threads(workers) if(workers > 1) schedule(static) reduction(|:invalid)
    for (int row = 0; row < rows; ++row) {
        const int pair = orbits[players[row]] * slots + bosses[row];
        const Rule* row_rules = rules.data() + pair * action_count;
        boss_weight[row] = weights[pair];
        const int first_slot = first[row];
        const int second_slot = second[row];
        auto* targets = own_next + row * own_actions;
        for (int action = 0; action < action_count; ++action) {
            if (action == own_actions) targets = boss_next + row * 16;
            const Rule rule = row_rules[action];
            int packed = rule.state_base;
            if (rule.state_base >= 0) {
                const auto* map = canonical_maps + rule.map_index * slot_stride;
                const bool take_i = first_slot == rule.pickup;
                const bool take_j = second_slot == rule.pickup;
                const int next_i = take_i ? slots : map[first_slot];
                const int next_j = take_j ? slots : map[second_slot];
                const int placement = placement_ids[next_i * slot_stride + next_j];
                const int target = placement < 0 ? -1 : state_ids[rule.state_base + placement];
                packed = 2 * target + (take_i || take_j);
                invalid |= (target < 0) || (take_i && take_j);
            }
            *targets++ = packed;
        }
    }
    return invalid ? 2 : 0;
} catch (const std::bad_alloc&) {
    return -1;
}
