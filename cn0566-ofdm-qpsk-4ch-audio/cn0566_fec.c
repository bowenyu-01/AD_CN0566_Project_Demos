#include <float.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/*
 * Native helpers for the CN0566 four-stream OFDM project.
 *
 * 1) Rate-1/2, K=7 convolutional codec
 *    Polynomials: 0o133 and 0o171. Positive soft evidence favors encoded zero.
 *
 * 2) Independent 320-sample IMA-ADPCM block encoder/decoder
 *    Payload layout is byte-for-byte compatible with cn0566_ofdm_core.py:
 *      little-endian int16 initial predictor, uint8 index, uint8 reserved,
 *      followed by low-nibble-first packed IMA codes.
 */

#define K 7
#define STATES 64
#define G0 0133
#define G1 0171
#define AUDIO_SAMPLES 320
#define ADPCM_HEADER_BYTES 4
#define ADPCM_CODE_BYTES 160
#define ADPCM_PAYLOAD_BYTES (ADPCM_HEADER_BYTES + ADPCM_CODE_BYTES)

static inline uint8_t parity7(uint8_t value) {
    value ^= value >> 4;
    value ^= value >> 2;
    value ^= value >> 1;
    return value & 1u;
}

int cn0566_conv_encoded_length(int input_bits) {
    if (input_bits < 0) return -1;
    return 2 * (input_bits + K - 1);
}

int cn0566_conv_encode(const uint8_t *input, int input_bits, uint8_t *output, int output_capacity) {
    if (!input || !output || input_bits < 0) return -1;
    const int steps = input_bits + K - 1;
    const int required = 2 * steps;
    if (output_capacity < required) return -2;

    uint8_t state = 0;
    for (int i = 0; i < steps; ++i) {
        uint8_t bit = (i < input_bits) ? (input[i] & 1u) : 0u;
        uint8_t reg = (uint8_t)(((state << 1) | bit) & 0x7fu);
        output[2 * i] = parity7(reg & G0);
        output[2 * i + 1] = parity7(reg & G1);
        state = reg & 0x3fu;
    }
    return required;
}

/*
 * Decode soft evidence. soft[2*i] and soft[2*i+1] are signed values;
 * positive favors encoded zero, negative favors encoded one.
 * The encoder is assumed terminated to state zero with K-1 zero bits.
 * Returns number of information bits (tail removed), or negative error code.
 */
/* Optimized fixed-trellis soft Viterbi decoder.
 *
 * Two implementation details matter on Raspberry Pi:
 * 1) Each next state has exactly two predecessors.  Evaluating those two
 *    candidates directly avoids the generic 64-state x 2-input branch loop.
 * 2) Survivor storage is retained per decoder thread instead of allocating and
 *    freeing roughly 200 workspaces per second in the four-stream receiver.
 *
 * GCC's __thread storage is available on the ARM Linux targets used here and
 * keeps concurrent channel decoders independent.
 */
typedef struct {
    int capacity_steps;
    uint8_t *survivor_prev;
    uint8_t *survivor_bit;
    uint8_t *all_bits;
} viterbi_workspace_t;

static __thread viterbi_workspace_t viterbi_workspace = {0, NULL, NULL, NULL};

static int ensure_viterbi_workspace(int steps) {
    if (steps <= viterbi_workspace.capacity_steps) return 0;

    uint8_t *new_prev = (uint8_t *)realloc(
        viterbi_workspace.survivor_prev, (size_t)steps * STATES);
    if (!new_prev) return -1;
    viterbi_workspace.survivor_prev = new_prev;

    uint8_t *new_bit = (uint8_t *)realloc(
        viterbi_workspace.survivor_bit, (size_t)steps * STATES);
    if (!new_bit) return -1;
    viterbi_workspace.survivor_bit = new_bit;

    uint8_t *new_all = (uint8_t *)realloc(
        viterbi_workspace.all_bits, (size_t)steps);
    if (!new_all) return -1;
    viterbi_workspace.all_bits = new_all;
    viterbi_workspace.capacity_steps = steps;
    return 0;
}

int cn0566_viterbi_decode(const float *soft, int coded_bits,
                          uint8_t *output, int output_capacity) {
    if (!soft || !output || coded_bits <= 0 || (coded_bits & 1)) return -1;
    const int steps = coded_bits / 2;
    const int info_bits = steps - (K - 1);
    if (info_bits < 0 || output_capacity < info_bits) return -2;
    if (ensure_viterbi_workspace(steps) != 0) return -3;

    uint8_t *survivor_prev = viterbi_workspace.survivor_prev;
    uint8_t *survivor_bit = viterbi_workspace.survivor_bit;
    uint8_t *all_bits = viterbi_workspace.all_bits;

    uint8_t predecessor0[STATES];
    uint8_t predecessor1[STATES];
    float sign00[STATES], sign01[STATES], sign10[STATES], sign11[STATES];

    for (int next = 0; next < STATES; ++next) {
        const uint8_t bit = (uint8_t)(next & 1);
        const uint8_t p0 = (uint8_t)(next >> 1);
        const uint8_t p1 = (uint8_t)(p0 | 0x20u);
        const uint8_t reg0 = (uint8_t)(((p0 << 1) | bit) & 0x7fu);
        const uint8_t reg1 = (uint8_t)(((p1 << 1) | bit) & 0x7fu);
        predecessor0[next] = p0;
        predecessor1[next] = p1;
        sign00[next] = parity7(reg0 & G0) ? -1.0f : 1.0f;
        sign01[next] = parity7(reg0 & G1) ? -1.0f : 1.0f;
        sign10[next] = parity7(reg1 & G0) ? -1.0f : 1.0f;
        sign11[next] = parity7(reg1 & G1) ? -1.0f : 1.0f;
    }

    float metric[STATES];
    float next_metric[STATES];
    for (int state = 0; state < STATES; ++state)
        metric[state] = (state == 0) ? 0.0f : FLT_MAX / 8.0f;

    for (int t = 0; t < steps; ++t) {
        const float y0 = soft[2 * t];
        const float y1 = soft[2 * t + 1];
        float minimum = FLT_MAX;

        for (int next = 0; next < STATES; ++next) {
            const uint8_t p0 = predecessor0[next];
            const uint8_t p1 = predecessor1[next];
            const float candidate0 = metric[p0] -
                (sign00[next] * y0 + sign01[next] * y1);
            const float candidate1 = metric[p1] -
                (sign10[next] * y0 + sign11[next] * y1);

            if (candidate0 <= candidate1) {
                next_metric[next] = candidate0;
                survivor_prev[(size_t)t * STATES + next] = p0;
            } else {
                next_metric[next] = candidate1;
                survivor_prev[(size_t)t * STATES + next] = p1;
            }
            survivor_bit[(size_t)t * STATES + next] = (uint8_t)(next & 1);
            if (next_metric[next] < minimum) minimum = next_metric[next];
        }

        for (int state = 0; state < STATES; ++state)
            metric[state] = next_metric[state] - minimum;
    }

    uint8_t state = 0; /* terminated encoder */
    for (int t = steps - 1; t >= 0; --t) {
        all_bits[t] = survivor_bit[(size_t)t * STATES + state];
        state = survivor_prev[(size_t)t * STATES + state];
    }
    memcpy(output, all_bits, (size_t)info_bits);
    return info_bits;
}

static const int8_t ima_index_table[16] = {
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8
};

static const int16_t ima_step_table[89] = {
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371,
    408, 449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166,
    1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024,
    3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845,
    8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500,
    20350, 22385, 24623, 27086, 29794, 32767
};

static inline int clamp_int(int value, int low, int high) {
    if (value < low) return low;
    if (value > high) return high;
    return value;
}

static inline uint8_t ima_encode_nibble(int sample, int *predictor, int *index) {
    const int step = ima_step_table[*index];
    int difference = sample - *predictor;
    int code = 0;
    if (difference < 0) {
        code = 8;
        difference = -difference;
    }

    int delta = step >> 3;
    if (difference >= step) {
        code |= 4;
        difference -= step;
        delta += step;
    }
    if (difference >= (step >> 1)) {
        code |= 2;
        difference -= step >> 1;
        delta += step >> 1;
    }
    if (difference >= (step >> 2)) {
        code |= 1;
        delta += step >> 2;
    }

    *predictor = (code & 8) ? (*predictor - delta) : (*predictor + delta);
    *predictor = clamp_int(*predictor, -32768, 32767);
    *index = clamp_int(*index + ima_index_table[code & 0x0f], 0, 88);
    return (uint8_t)(code & 0x0f);
}

static inline int ima_decode_nibble(uint8_t code, int *predictor, int *index) {
    const int step = ima_step_table[*index];
    int delta = step >> 3;
    if (code & 4u) delta += step;
    if (code & 2u) delta += step >> 1;
    if (code & 1u) delta += step >> 2;
    *predictor = (code & 8u) ? (*predictor - delta) : (*predictor + delta);
    *predictor = clamp_int(*predictor, -32768, 32767);
    *index = clamp_int(*index + ima_index_table[code & 0x0f], 0, 88);
    return *predictor;
}

/* Return payload bytes written (164), or a negative error code. */
int cn0566_ima_adpcm_encode(const int16_t *samples, int sample_count,
                            uint8_t *output, int output_capacity) {
    if (!samples || !output || sample_count != AUDIO_SAMPLES) return -1;
    if (output_capacity < ADPCM_PAYLOAD_BYTES) return -2;

    memset(output, 0, (size_t)ADPCM_PAYLOAD_BYTES);
    int predictor = samples[0];
    int index = 0;
    output[0] = (uint8_t)(predictor & 0xff);
    output[1] = (uint8_t)(((uint16_t)predictor >> 8) & 0xff);
    output[2] = 0;
    output[3] = 0;

    for (int i = 1; i < AUDIO_SAMPLES; ++i) {
        const uint8_t code = ima_encode_nibble(samples[i], &predictor, &index);
        const int nibble_index = i - 1;
        const int byte_index = ADPCM_HEADER_BYTES + nibble_index / 2;
        if (nibble_index & 1) {
            output[byte_index] |= (uint8_t)(code << 4);
        } else {
            output[byte_index] = code;
        }
    }
    return ADPCM_PAYLOAD_BYTES;
}

/* Return output samples written (320), or a negative error code. */
int cn0566_ima_adpcm_decode(const uint8_t *payload, int payload_bytes,
                            int valid_samples, int16_t *output, int output_capacity) {
    if (!payload || !output || payload_bytes != ADPCM_PAYLOAD_BYTES) return -1;
    if (output_capacity < AUDIO_SAMPLES) return -2;
    valid_samples = clamp_int(valid_samples, 0, AUDIO_SAMPLES);
    memset(output, 0, (size_t)AUDIO_SAMPLES * sizeof(int16_t));
    if (valid_samples == 0) return AUDIO_SAMPLES;

    int predictor = (int16_t)((uint16_t)payload[0] | ((uint16_t)payload[1] << 8));
    int index = clamp_int(payload[2], 0, 88);
    output[0] = (int16_t)predictor;
    int position = 1;
    for (int byte_index = ADPCM_HEADER_BYTES;
         byte_index < ADPCM_PAYLOAD_BYTES && position < valid_samples;
         ++byte_index) {
        const uint8_t value = payload[byte_index];
        const uint8_t codes[2] = {(uint8_t)(value & 0x0f), (uint8_t)((value >> 4) & 0x0f)};
        for (int half = 0; half < 2 && position < valid_samples; ++half) {
            output[position++] = (int16_t)ima_decode_nibble(codes[half], &predictor, &index);
        }
    }
    return AUDIO_SAMPLES;
}
