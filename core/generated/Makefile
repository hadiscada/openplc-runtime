# Paths
LIBPATH   := core/generated/plc_lib/lib
SRCPATH   := core/generated/plc_lib

# Compiler and flags
CC        := gcc
CFLAGS    := -pedantic -Wextra -fstack-protector-strong -D_FORTIFY_SOURCE=2 \
             -O3 -Wformat -Werror=format-security -fPIC -fPIE -I$(LIBPATH)

# Sources and objects
SRCS      := $(SRCPATH)/Config0.c $(SRCPATH)/Res0.c $(SRCPATH)/debug.c glueVars.c
OBJS      := $(SRCS:.c=.o)

# Target
TARGET    := libplc.so

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) -shared -o $@ $(OBJS) $(CFLAGS)
	mv $@ ..

%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

clean:
	rm -f $(OBJS) ../$(TARGET) $(TARGET)

.PHONY: all clean
