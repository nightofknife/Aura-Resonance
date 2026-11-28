#include <algorithm>
#include <cstdint>

// Each state is a canonical ordered pair of positions. The last two columns
// of every probability layer are the absorbing encounter and illegal edge.
extern "C" __declspec(dllexport) void player_quota_values(
    const std::int32_t* move_edges, const std::int32_t* rotation_edges,
    int rows, int move_columns, int moves, int rotations, double* layers
) {
    const int width = rows + 2;
    const int rotation_stride = (rotations + 1) * width;
    for (int m = 0; m <= moves; ++m) {
        for (int r = 0; r <= rotations; ++r) {
            if (m == 0 && r == 0) continue;
            double* output = layers + m * rotation_stride + r * width;
            const double* after_move = m > 0 ? output - rotation_stride : nullptr;
            const double* after_rotation = r > 0 ? output - width : nullptr;
            for (int state = 0; state < rows; ++state) {
                double best = 0.;
                if (after_move) {
                    const auto* edges = move_edges + state * move_columns;
                    for (int col = 0; col < move_columns; ++col) {
                        best = std::max(best, after_move[edges[col]]);
                    }
                }
                if (after_rotation) {
                    const auto* edges = rotation_edges + state * 4;
                    for (int col = 0; col < 4; ++col) {
                        best = std::max(best, after_rotation[edges[col]]);
                    }
                }
                output[state] = best;
            }
            output[rows] = 1.;
            output[rows + 1] = 0.;
        }
    }
}
