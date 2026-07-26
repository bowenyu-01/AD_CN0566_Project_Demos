#include <float.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define STATES 64
#define G0 0133
#define G1 0171

static unsigned parity7(unsigned value) {
    value ^= value >> 4;
    value ^= value >> 2;
    value ^= value >> 1;
    return value & 1U;
}

/*
 * Soft-decision rate-1/2, K=7 Viterbi decoder.
 * soft values use the project convention: positive favors encoded bit 0,
 * negative favors encoded bit 1.
 *
 * Returns 0 on success and a negative value on invalid input/allocation failure.
 */
int cn0566_viterbi_decode(
    const float *soft,
    int coded_bits,
    uint8_t *decoded,
    int decoded_bits
) {
    if (!soft || !decoded || coded_bits <= 0 || decoded_bits <= 0) return -1;
    if (coded_bits != 2 * decoded_bits) return -2;

    uint8_t *survivor_state = (uint8_t *)malloc((size_t)decoded_bits * STATES);
    uint8_t *survivor_bit = (uint8_t *)malloc((size_t)decoded_bits * STATES);
    if (!survivor_state || !survivor_bit) {
        free(survivor_state);
        free(survivor_bit);
        return -3;
    }

    float metric[STATES];
    float next_metric[STATES];
    for (int s = 0; s < STATES; ++s) metric[s] = FLT_MAX / 8.0f;
    metric[0] = 0.0f;

    for (int t = 0; t < decoded_bits; ++t) {
        for (int s = 0; s < STATES; ++s) next_metric[s] = FLT_MAX / 8.0f;
        const float r0 = soft[2 * t];
        const float r1 = soft[2 * t + 1];

        for (int state = 0; state < STATES; ++state) {
            const float pm = metric[state];
            if (pm >= FLT_MAX / 16.0f) continue;
            for (int bit = 0; bit <= 1; ++bit) {
                const unsigned reg = ((unsigned)state << 1) | (unsigned)bit;
                const int out0 = (int)parity7(reg & G0);
                const int out1 = (int)parity7(reg & G1);
                const float e0 = out0 ? -1.0f : 1.0f;
                const float e1 = out1 ? -1.0f : 1.0f;
                const float d0 = r0 - e0;
                const float d1 = r1 - e1;
                const float bm = d0 * d0 + d1 * d1;
                const int ns = (int)(reg & 0x3FU);
                const float candidate = pm + bm;
                if (candidate < next_metric[ns]) {
                    next_metric[ns] = candidate;
                    survivor_state[(size_t)t * STATES + ns] = (uint8_t)state;
                    survivor_bit[(size_t)t * STATES + ns] = (uint8_t)bit;
                }
            }
        }
        memcpy(metric, next_metric, sizeof(metric));
    }

    /* The encoder is terminated with six zero bits, so state zero is expected. */
    int state = 0;
    if (metric[state] >= FLT_MAX / 16.0f) {
        state = 0;
        for (int s = 1; s < STATES; ++s) {
            if (metric[s] < metric[state]) state = s;
        }
    }

    for (int t = decoded_bits - 1; t >= 0; --t) {
        const size_t idx = (size_t)t * STATES + (size_t)state;
        decoded[t] = survivor_bit[idx];
        state = survivor_state[idx];
    }

    free(survivor_state);
    free(survivor_bit);
    return 0;
}
